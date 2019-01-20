import abc
import asyncio
import datetime
import enum
import os
import re
import time
from logging import Handler, LogRecord
from typing import Callable, List

import aiofiles
from aiofiles.threadpool import AsyncTextIOWrapper

from aiologger.handlers.streams import AsyncStreamHandler
from aiologger.utils import classproperty


class AsyncFileHandler(AsyncStreamHandler):
    def __init__(
        self, filename: str, mode: str = "a", encoding: str = None
    ) -> None:
        filename = os.fspath(filename)
        self.absolute_file_path = os.path.abspath(filename)
        self.mode = mode
        self.encoding = encoding
        self.stream: AsyncTextIOWrapper = None
        self._intialization_lock = asyncio.Lock()
        Handler.__init__(self)

    @property
    def initialized(self):
        return self.stream is not None

    async def _init_writer(self):
        """
        Open the current base file with the (original) mode and encoding.
        Return the resulting stream.
        """
        async with self._intialization_lock:
            if not self.initialized:
                self.stream = await aiofiles.open(
                    file=self.absolute_file_path,
                    mode=self.mode,
                    encoding=self.encoding,
                )

    async def close(self):
        if not self.stream:
            return
        if hasattr(self.stream, "flush"):
            await self.stream.flush()
        await self.stream.close()

    async def emit(self, record: LogRecord):  # type: ignore
        if not self.initialized:
            await self._init_writer()

        try:
            msg = self.format(record) + self.terminator
            await self.stream.write(msg)
            await self.stream.flush()
        except Exception as e:
            await self.handleError(record)


Namer = Callable[[str], str]
Rotator = Callable[[str, str], None]


class BaseAsyncRotatingFileHandler(AsyncFileHandler, metaclass=abc.ABCMeta):
    def __init__(
        self,
        filename: str,
        mode: str = "a",
        encoding: str = None,
        namer: Namer = None,
        rotator: Rotator = None,
    ) -> None:
        super().__init__(filename, mode, encoding)
        self.mode = mode
        self.encoding = encoding
        self.namer = namer
        self.rotator = rotator
        self._rollover_lock = asyncio.Lock()

    def should_rollover(self, record: LogRecord) -> bool:
        raise NotImplementedError

    async def do_rollover(self):
        raise NotImplementedError

    async def emit(self, record: LogRecord):  # type: ignore
        """
        Emit a record.

        Output the record to the file, catering for rollover as described
        in `do_rollover`.
        """
        try:
            if self.should_rollover(record):
                async with self._rollover_lock:
                    if self.should_rollover(record):
                        await self.do_rollover()
            await super().emit(record)
        except Exception as e:
            await self.handleError(record)

    def rotation_filename(self, default_name: str) -> str:
        """
        Modify the filename of a log file when rotating.

        This is provided so that a custom filename can be provided.

        :param default_name: The default name for the log file.
        """
        if self.namer is None:
            return default_name

        return self.namer(default_name)

    def rotate(self, source: str, dest: str):
        """
        When rotating, rotate the current log.

        The default implementation calls the 'rotator' attribute of the
        handler, if it's callable, passing the source and dest arguments to
        it. If the attribute isn't callable (the default is None), the source
        is simply renamed to the destination.

        :param source: The source filename. This is normally the base
                       filename, e.g. 'test.log'
        :param dest:   The destination filename. This is normally
                       what the source is rotated to, e.g. 'test.log.1'.
        """
        if self.rotator is None:
            # logging issue 18940: A file may not have been created if delay is True.
            if os.path.exists(source):
                os.rename(source, dest)
        else:
            self.rotator(source, dest)


class AsyncRotatingFileHandler(BaseAsyncRotatingFileHandler):
    """
    Handler for logging to a set of files, which switches from one file
    to the next when the current file reaches a certain size.
    """

    def __init__(
        self,
        filename: str,
        mode: str = "a",
        max_bytes: int = 0,
        backup_count: int = 0,
        encoding: str = None,
        namer: Namer = None,
        rotator: Rotator = None,
    ) -> None:
        """
        Open the specified file and use it as the stream for logging.

        By default, the file grows indefinitely. You can specify particular
        values of `max_bytes` and `backup_count` to allow the file to rollover
        at a predetermined size.

        Rollover occurs whenever the current log file is nearly `max_bytes` in
        length. If `backup_count` is >= 1, the system will successively create
        new files with the same pathname as the base file, but with extensions
        ".1", ".2" etc. appended to it. For example, with a `backup_count` of 5
        and a base file name of "app.log", you would get "app.log",
        "app.log.1", "app.log.2", ... through to "app.log.5". The file being
        written to is always "app.log" - when it gets filled up, it is closed
        and renamed to "app.log.1", and if files "app.log.1", "app.log.2" etc.
        exist, then they are renamed to "app.log.2", "app.log.3" etc.
        respectively.

        If `max_bytes` is zero, rollover never occurs.
        """
        if max_bytes > 0 and mode != "a":
            # If rotation/rollover is wanted, it doesn't make sense to use
            # another mode. If for example 'w' were specified, then if there
            # were multiple runs of the calling application, the logs from
            # previous runs would be lost if the 'w' is respected, because the
            # log file would be truncated on each run.
            raise ValueError("max_bytes can't be set > 0 if mode isn't \"a\"")
        super().__init__(filename, mode, encoding, namer, rotator)
        self.max_bytes = max_bytes
        self.backup_count = backup_count


class RolloverInterval(str, enum.Enum):
    SECONDS = "S"
    MINUTES = "M"
    HOURS = "H"
    DAYS = "D"
    MONDAYS = "W0"
    TUESDAYS = "W1"
    WEDNESDAYS = "W2"
    THUERDAYS = "W3"
    FRIDAYS = "W4"
    SATURDAYS = "W5"
    SUNDAYS = "W6"
    MIDNIGHT = "MIDNIGHT"

    @classproperty
    def WEEK_DAYS(cls):
        return (
            cls.MONDAYS,
            cls.TUESDAYS,
            cls.WEDNESDAYS,
            cls.THUERDAYS,
            cls.FRIDAYS,
            cls.SATURDAYS,
            cls.SUNDAYS,
        )


ONE_MINUTE_IN_SECONDS = 60
ONE_HOUR_IN_SECONDS = 60 * 60
ONE_DAY_IN_SECONDS = ONE_HOUR_IN_SECONDS * 24
ONE_WEEK_IN_SECONDS = 7 * ONE_DAY_IN_SECONDS


class AsyncTimedRotatingFileHandler(BaseAsyncRotatingFileHandler):
    """
    Handler for logging to a file, rotating the log file at certain timed
    intervals.

    If `backup_count` is > 0, when rollover is done, no more than `backup_count`
    files are kept - the oldest ones are deleted.
    """

    def __init__(
        self,
        filename: str,
        when: RolloverInterval = RolloverInterval.HOURS,
        interval: int = 1,
        backup_count: int = 0,
        encoding: str = None,
        utc: bool = False,
        at_time: datetime.time = None,
    ) -> None:
        super().__init__(filename=filename, mode="a", encoding=encoding)
        self.when = when.upper()
        self.backup_count = backup_count
        self.utc = utc
        self.at_time = at_time
        # Calculate the real rollover interval, which is just the number of
        # seconds between rollovers.  Also set the filename suffix used when
        # a rollover occurs.  Current 'when' events supported:
        # S - Seconds
        # M - Minutes
        # H - Hours
        # D - Days
        # midnight - roll over at midnight
        # W{0-6} - roll over on a certain day; 0 - Monday
        #
        # Case of the 'when' specifier is not important; lower or upper case
        # will work.
        if self.when == RolloverInterval.SECONDS:
            self.interval = 1  # one second
            self.suffix = "%Y-%m-%d_%H-%M-%S"
            ext_match = r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}(\.\w+)?$"
        elif self.when == RolloverInterval.MINUTES:
            self.interval = ONE_MINUTE_IN_SECONDS  # one minute
            self.suffix = "%Y-%m-%d_%H-%M"
            ext_match = r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}(\.\w+)?$"
        elif self.when == RolloverInterval.HOURS:
            self.interval = ONE_HOUR_IN_SECONDS  # one hour
            self.suffix = "%Y-%m-%d_%H"
            ext_match = r"^\d{4}-\d{2}-\d{2}_\d{2}(\.\w+)?$"
        elif (
            self.when == RolloverInterval.DAYS
            or self.when == RolloverInterval.MIDNIGHT
        ):
            self.interval = ONE_DAY_IN_SECONDS  # one day
            self.suffix = "%Y-%m-%d"
            ext_match = r"^\d{4}-\d{2}-\d{2}(\.\w+)?$"
        elif self.when.startswith("W"):
            if self.when not in RolloverInterval.WEEK_DAYS:
                raise ValueError(
                    f"Invalid day specified for weekly rollover: {self.when}"
                )
            self.interval = ONE_DAY_IN_SECONDS * 7  # one week
            self.day_of_week = int(self.when[1])
            self.suffix = "%Y-%m-%d"
            ext_match = r"^\d{4}-\d{2}-\d{2}(\.\w+)?$"
        else:
            raise ValueError(f"Invalid RolloverInterval specified: {self.when}")

        self.ext_match = re.compile(ext_match, re.ASCII)
        self.interval = self.interval * interval  # multiply by units requested
        # The following line added because the filename passed in could be a
        # path object (see Issue #27493), but self.baseFilename will be a string
        filename = self.absolute_file_path
        if os.path.exists(filename):
            t = int(os.stat(filename).st_mtime)
        else:
            t = int(time.time())
        self.rollover_at = self.compute_rollover(t)

    def compute_rollover(self, current_time: int) -> int:
        """
        Work out the rollover time based on the specified time.

        If we are rolling over at midnight or weekly, then the interval is
        already known. need to figure out is WHEN the next interval is.
        In other words, if you are rolling over at midnight, then your base
        interval is 1 day, but you want to start that one day clock at midnight,
         not now. So, we have to fudge the `rollover_at` value in order to trigger
         the first rollover at the right time.  After that, the regular interval
         will take care of the rest.  Note that this code doesn't care about
         leap seconds. :)
        """
        result = current_time + self.interval

        if (
            self.when == RolloverInterval.MIDNIGHT
            or self.when in RolloverInterval.WEEK_DAYS
        ):
            if self.utc:
                t = time.gmtime(current_time)
            else:
                t = time.localtime(current_time)
            current_hour = t[3]
            current_minute = t[4]
            current_second = t[5]
            current_day = t[6]
            # r is the number of seconds left between now and the next rotation
            if self.at_time is None:
                rotate_ts = ONE_DAY_IN_SECONDS
            else:
                rotate_ts = (
                    self.at_time.hour * 60 + self.at_time.minute
                ) * 60 + self.at_time.second

            r = rotate_ts - (
                (current_hour * 60 + current_minute) * 60 + current_second
            )
            if r < 0:
                # Rotate time is before the current time (for example when
                # self.rotateAt is 13:45 and it now 14:15), rotation is
                # tomorrow.
                r += ONE_DAY_IN_SECONDS
                current_day = (current_day + 1) % 7
            result = current_time + r
            # If we are rolling over on a certain day, add in the number of days until
            # the next rollover, but offset by 1 since we just calculated the time
            # until the next day starts.  There are three cases:
            # Case 1) The day to rollover is today; in this case, do nothing
            # Case 2) The day to rollover is further in the interval (i.e., today is
            #         day 2 (Wednesday) and rollover is on day 6 (Sunday).  Days to
            #         next rollover is simply 6 - 2 - 1, or 3.
            # Case 3) The day to rollover is behind us in the interval (i.e., today
            #         is day 5 (Saturday) and rollover is on day 3 (Thursday).
            #         Days to rollover is 6 - 5 + 3, or 4.  In this case, it's the
            #         number of days left in the current week (1) plus the number
            #         of days in the next week until the rollover day (3).
            # The calculations described in 2) and 3) above need to have a day added.
            # This is because the above time calculation takes us to midnight on this
            # day, i.e. the start of the next day.
            if self.when in RolloverInterval.WEEK_DAYS:
                day = current_day  # 0 is Monday
                if day != self.day_of_week:
                    if day < self.day_of_week:
                        days_to_wait = self.day_of_week - day
                    else:
                        days_to_wait = 6 - day + self.day_of_week + 1
                    new_rollover_at = result + (days_to_wait * (60 * 60 * 24))
                    if not self.utc:
                        dst_now = t[-1]
                        dst_at_rollover = time.localtime(new_rollover_at)[-1]
                        if dst_now != dst_at_rollover:
                            if (
                                not dst_now
                            ):  # DST kicks in before next rollover, so we need to deduct an hour
                                addend = -3600
                            else:  # DST bows out before next rollover, so we need to add an hour
                                addend = 3600
                            new_rollover_at += addend
                    result = new_rollover_at
        return result

    def should_rollover(self, record: LogRecord) -> bool:
        """
        Determine if rollover should occur.

        record is not used, as we are just comparing times, but it is needed so
        the method signatures are the same
        """
        t = int(time.time())
        if t >= self.rollover_at:
            return True
        return False

    def get_files_to_delete(self) -> List[str]:
        """
        Determine the files to delete when rolling over.

        More specific than the earlier method, which just used glob.glob().
        """
        dir_name, base_name = os.path.split(self.absolute_file_path)
        file_names = os.listdir(dir_name)  # todo: IO
        result = []
        prefix = base_name + "."
        plen = len(prefix)
        for file_name in file_names:
            if file_name[:plen] == prefix:
                suffix = file_name[plen:]
                if self.ext_match.match(suffix):
                    result.append(os.path.join(dir_name, file_name))
        if len(result) < self.backup_count:
            result = []
        else:
            result.sort()
            result = result[: len(result) - self.backup_count]
        return result

    async def do_rollover(self):
        """
        do a rollover; in this case, a date/time stamp is appended to the filename
        when the rollover happens.  However, you want the file to be named for the
        start of the interval, not the current time.  If there is a backup count,
        then we have to get a list of matching filenames, sort them and remove
        the one with the oldest suffix.
        """
        if self.stream:
            await self.stream.close()
            self.stream = None
        # get the time that this sequence started at and make it a TimeTuple
        current_time = int(time.time())
        dst_now = time.localtime(current_time)[-1]
        t = self.rollover_at - self.interval
        if self.utc:
            time_tuple = time.gmtime(t)
        else:
            time_tuple = time.localtime(t)
            dstThen = time_tuple[-1]
            if dst_now != dstThen:
                if dst_now:
                    addend = 3600
                else:
                    addend = -3600
                time_tuple = time.localtime(t + addend)
        dfn = self.rotation_filename(
            self.absolute_file_path
            + "."
            + time.strftime(self.suffix, time_tuple)
        )
        if os.path.exists(dfn):  # todo: IO
            os.remove(dfn)  # todo: IO
        self.rotate(self.absolute_file_path, dfn)
        if self.backup_count > 0:
            for s in self.get_files_to_delete():
                os.remove(s)  # todo: IO

        await self._init_writer()
        new_rollover_at = self.compute_rollover(current_time)
        while new_rollover_at <= current_time:
            new_rollover_at = new_rollover_at + self.interval
        # If DST changes and midnight or weekly rollover, adjust for this.
        if (
            self.when == RolloverInterval.MIDNIGHT
            or self.when in RolloverInterval.WEEK_DAYS
        ) and not self.utc:
            dst_at_rollover = time.localtime(new_rollover_at)[-1]
            if dst_now != dst_at_rollover:
                if (
                    not dst_now
                ):  # DST kicks in before next rollover, so we need to deduct an hour
                    addend = -3600
                else:  # DST bows out before next rollover, so we need to add an hour
                    addend = 3600
                new_rollover_at += addend
        self.rollover_at = new_rollover_at