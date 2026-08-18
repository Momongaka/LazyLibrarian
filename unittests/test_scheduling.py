#  This file is part of Lazylibrarian.
#
# Purpose:
#   Test functions in scheduling.py

import time
import datetime
import logging

from lazylibrarian import scheduling
from lazylibrarian.scheduling import SchedulerCommand
from unittests.unittesthelpers import LLTestCaseWithStartup


class SchedulingTest(LLTestCaseWithStartup):

    def test_next_run_time(self):
        testnow = datetime.datetime(2023, 11, 30, 17, 42, 31)
        testcases = [
            (datetime.timedelta(seconds=0), "0 seconds"),
            (datetime.timedelta(seconds=1), "1 second"),
            (datetime.timedelta(seconds=10), "10 seconds"),
            (datetime.timedelta(seconds=60), "60 seconds"),
            (datetime.timedelta(seconds=100), "2 minutes"),
            (datetime.timedelta(minutes=1), "60 seconds"),
            (datetime.timedelta(hours=1), "60 minutes"),
            (datetime.timedelta(hours=10), "10 hours"),
            (datetime.timedelta(days=1, seconds=1), "2 days"),
            (datetime.timedelta(days=1, hours=1), "2 days"),
            (datetime.timedelta(hours=100), "4 days"),
            (datetime.timedelta(days=1), "24 hours"),
            (datetime.timedelta(hours=25), "2 days"),
            (datetime.timedelta(days=2), "2 days"),
            (datetime.timedelta(days=100), "100 days"),
            (datetime.timedelta(days=1000), "1000 days"),
            (datetime.timedelta(weeks=5, days=7), "42 days"),
            # Failure case: Don't supply increments of less than a second
            (datetime.timedelta(days=1, microseconds=1), "0 seconds"),
        ]
        with self.assertLogs(None, logging.ERROR) as logs:  # The failure case will log an error
            for case in testcases:
                delta = case[0]
                nrt = scheduling.next_run_time(str(testnow + delta), testnow)
                self.assertEqual(nrt, case[1])
        self.assertEqual(len(logs.output), 1, 'Expected just 1 error')

    def test_get_next_run_time(self):
        # Schedule a job every 10 minutes, or 600 seconds. The job will never have run before
        startdate = scheduling.get_next_run_time('fakescheduled_job', 10, SchedulerCommand.START)
        # Check that the first start time seems plausible, with a bit of slack
        self.assertGreater(startdate, datetime.datetime.fromtimestamp(time.time()), 'start should be later than now')
        self.assertLess(startdate, datetime.datetime.fromtimestamp(time.time()+700), 'start seems too late')

        # Add the job to the schedule and start the scheduler
        scheduling.add_interval_job(fakescheduled_job, hours=0, minutes=10, startdate=startdate, target="fakescheduled_job")
        scheduling.startscheduler()

        # Do the same again, this time it should return the same time
        startdate1 = scheduling.get_next_run_time('fakescheduled_job', 10, SchedulerCommand.START)
        self.assertEqual(startdate, startdate1, 'Should be the same to the nearest second')

        # Should add testing for log messages


def fakescheduled_job():
    pass
