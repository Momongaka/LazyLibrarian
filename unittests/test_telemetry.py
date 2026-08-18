#  This file is part of Lazylibrarian.
#
# Purpose:
#   Testing the anaonymous telemetry collection

import json
import pytest
# import pytest_order  # Needed to force unit test order
import mock
import logging

from lazylibrarian.config2 import LLConfigHandler
from lazylibrarian import telemetry, configdefs
from unittests.unittesthelpers import LLTestCaseWithConfigandDIRS


class TelemetryTest(LLTestCaseWithConfigandDIRS):

    # Initialisation code that needs to run only once
    @classmethod
    def setUpClass(cls) -> None:
        cls.CONFIGFILE = './unittests/testdata/testconfig-nondefault.ini'
        super().setUpClass()
        telemetry.TELEMETRY.clear_usage_data()

    def _do_ids_match(self):
        self.set_loglevel(logging.INFO)
        t = telemetry.LazyTelemetry()
        loaded_id = self.cfg()['SERVER_ID']
        serverid = t.ensure_server_id(self.cfg())
        self.assertIsNotNone(serverid)
        if loaded_id:
            self.assertEqual(serverid, loaded_id)
        return serverid

    def test_getTelemetryObject(self):
        self.set_loglevel(logging.INFO)
        t = telemetry.LazyTelemetry()
        self.assertIsNotNone(t, 'Telemetry object must exist')
        t2 = telemetry.LazyTelemetry()
        self.assertEqual(t, t2, 'Telemetry object not acting as singleton')

    def test_ensure_server_id_generation(self):
        self.set_loglevel(logging.INFO)
        saved_id = self._do_ids_match()
        # Pretend we don't have an ID to ensure generation works
        telemetry.LazyTelemetry().clear_id(self.cfg())

        new_id = self._do_ids_match()
        self.assertNotEqual(saved_id, new_id, 'ID generation does not work')
        self.assertEqual(len(saved_id), len(new_id), 'Expect constant length IDs')

        # Restore to known good state
        telemetry.LazyTelemetry().clear_id(self.cfg())
        self.cfg()['SERVER_ID'] = saved_id
        check_id = self._do_ids_match()
        self.assertEqual(saved_id, check_id, 'Test logic is broken')

    @pytest.mark.order(after="test_ensure_server_id_generation")
    def test_ensure_server_id_persistence(self):
        self.set_loglevel(logging.INFO)
        my_id = self._do_ids_match()

        # Check we can read the new ID and test again
        with self.assertLogs(self.logger, logging.INFO):
            self.cfg().save_config_and_backup_old(section='Telemetry')

        # Test that writing went right
        cfg = LLConfigHandler(configdefs.BASE_DEFAULTS, self.cfg().configfilename)
        id_from_file = cfg['Server_id']
        self.assertEqual(my_id, id_from_file, 'ID written to config.ini does not match')

        self._do_ids_match()

    def test_set_install_data(self):
        t = telemetry.LazyTelemetry()
        t.set_install_data(self.cfg(), testing=True)
        srv = t.get_server_telemetry()
        self.assertIsInstance(srv, dict)
        self.assertEqual(srv['id'], self.cfg()['SERVER_ID'])
        self.assertIsInstance(srv['uptime_seconds'], int)
        self.assertEqual(srv['python_ver'], '3.11.0 (main, Oct 24 2022, 18:26:48) [MSC v.1933 64 bit (AMD64)]')

    def test_set_config_data(self):
        t = telemetry.LazyTelemetry()
        t.set_config_data(self.cfg())
        cfg = t.get_config_telemetry()

        self.assertIsInstance(cfg, dict)
        # Helpful to create new json_good data:
        # json_fromcfg = json.dumps(obj=cfg)
        # print(json_fromcfg)
        json_good = json.loads("""
            {"switches": "EBOOK_TAB COMIC_TAB SERIES_TAB API_ENABLED CALIBRE_USE_SERVER OPF_TAGS ",
            "params": "PRIMARY_OL IMP_CALIBREDB DOWNLOAD_DIR API_KEY ",
            "BOOK_API": "",
            "NEWZNAB": 1, "TORZNAB": 0, "RSS": 0, "IRC": 0, "GEN": 0, "APPRISE": 1}
        """)
        self.assertEqual(cfg, json_good, "Config not as expected. Check that ini file has not changed")

    def test_record_usage_data(self):
        t = telemetry.LazyTelemetry()
        usg = t.get_usage_telemetry()
        self.assertEqual(usg["API/getHelp"], 2)
        self.assertEqual(usg["web/test"], 1)

        # Test automated recording:
        t.record_usage_data()
        self.assertEqual(usg["test_telemetry/test_record_usage_data"], 1)

        jsoncfg = json.dumps(obj=usg)

    @pytest.mark.order(after="test_ensure_server_id_generation")
    @pytest.mark.order(after="test_ensure_server_id_persistence")
    @pytest.mark.order(after="test_set_config_data")
    @pytest.mark.order(before="test_record_usage_data")
    def test_construct_data_string(self):
        t = telemetry.LazyTelemetry()
        t.set_install_data(self.cfg(), testing=True)
        t.set_config_data(self.cfg())
        t.record_usage_data("API/getHelp")
        t.record_usage_data("web/test")
        t.record_usage_data("API/getHelp")
        t.record_usage_data("Download/NZB")
        s_got = dict()
        s_got['server'] = t.construct_data_string(send_config=False, send_usage=False)
        s_got['config'] = t.construct_data_string(send_config=True, send_usage=False, send_server=False)
        s_got['usage'] = t.construct_data_string(send_config=False, send_usage=True, send_server=False)
        s_expect = [
            ['server',
             'server={"id":"5f6300cc949542f0bcde1ea110ba46a8","uptime_seconds":0,"install_type":"",'
             '"version":"","os":"nt","python_ver":"3.11.0 (main, Oct 24 2022, 18:26:48) [MSC v.1933 64 bit (AMD64)]"}'],
            ['config',
             'config={"switches":"EBOOK_TAB COMIC_TAB SERIES_TAB API_ENABLED CALIBRE_USE_SERVER OPF_TAGS "'
             ',"params":"PRIMARY_OL IMP_CALIBREDB DOWNLOAD_DIR API_KEY ","BOOK_API":"","NEWZNAB":1,'
             '"TORZNAB":0,"RSS":0,"IRC":0,"GEN":0,"APPRISE":1}'],
            ['usage',
             'usage={"config2/save_config_and_backup_old":1,"API/getHelp":2,"web/test":1,"Download/NZB":1}'],
        ]
        # Test individual strings
        for expect in s_expect:
            key = expect[0]
            self.assertEqual(key, s_got[key][:len(key)], 'Data string does not start with key')
            # Remove the key prefix before converting
            gotstr = s_got[key][len(key) + 1:]
            expstr = expect[1][len(key) + 1:]
            # Don't just compare strings, compare if they are equivalent
            # even if order of elements is different
            gotdata = json.loads(gotstr)
            expdata = json.loads(expstr)
            self.assertEqual(gotdata, expdata, f'Unexpected data for {key}')

        # Test they are concatenated correctly, excluding server key
        s_usage = t.construct_data_string(send_usage=True, send_config=True, send_server=False)
        print(1, s_usage)
        print(2, s_expect[1][1])
        print(3, s_expect[2][1])
        self.assertEqual(s_usage, f"{s_expect[1][1]}&{s_expect[2][1]}", 'Strings concatenated incorrectly')

    @pytest.mark.order(after="test_construct_data_string")
    @mock.patch('lazylibrarian.telemetry.requests')
    def test_submit_data(self, mock_requests):
        import requests

        t = telemetry.LazyTelemetry()

        # Pretend to submit data and experience a timeout
        with self.assertLogs(self.logger, logging.INFO):
            mock.side_effect = requests.exceptions.Timeout
            msg, status = t.submit_data('http://testserver', False, False)
            mock_requests.get.assert_called_once()
            self.assertFalse(status)

        # Pretend to submit data to the server successfully
        mock_requests.get.return_value.status_code = 200
        msg, status = t.submit_data('http://testserver', False, False)
        self.assertEqual(mock_requests.get.call_count, 2, "request.get() was not called")
        _ = mock_requests.get.call_args[0][0]
        _ = t.get_data_url(server='', send_config=True, send_usage=False)
        # self.assertEqual(URLarg, ExpectedURL, "Request URL not as expected")
        self.assertTrue(status, "Request call did not succeed")
