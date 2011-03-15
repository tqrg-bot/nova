# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright 2010 OpenStack LLC.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""
Tests dealing with HTTP rate-limiting.
"""

import httplib
import json
import StringIO
import stubout
import time
import webob

from nova import test
from nova.api.openstack import limits
from nova.api.openstack.limits import Limit


TEST_LIMITS = [
    Limit("GET", "/delayed", "^/delayed", 1, limits.PER_MINUTE),
    Limit("POST", "*", ".*", 7, limits.PER_MINUTE),
    Limit("POST", "/servers", "^/servers", 3, limits.PER_MINUTE),
    Limit("PUT", "*", "", 10, limits.PER_MINUTE),
    Limit("PUT", "/servers", "^/servers", 5, limits.PER_MINUTE),
]


class LimiterTest(test.TestCase):
    """
    Tests for the in-memory `limits.Limiter` class.
    """

    def setUp(self):
        """Run before each test."""
        test.TestCase.setUp(self)
        self.time = 0.0
        self.stubs = stubout.StubOutForTesting()
        self.stubs.Set(limits.Limit, "_get_time", self._get_time)
        self.limiter = limits.Limiter(TEST_LIMITS)

    def tearDown(self):
        """Run after each test."""
        self.stubs.UnsetAll()

    def _get_time(self):
        """Return the "time" according to this test suite."""
        return self.time

    def _check(self, num, verb, url, username=None):
        """Check and yield results from checks."""
        for x in xrange(num):
            yield self.limiter.check_for_delay(verb, url, username)

    def _check_sum(self, num, verb, url, username=None):
        """Check and sum results from checks."""
        results = self._check(num, verb, url, username)
        return sum(filter(lambda x: x != None, results))

    def test_no_delay_GET(self):
        """
        Simple test to ensure no delay on a single call for a limit verb we
        didn"t set.
        """
        delay = self.limiter.check_for_delay("GET", "/anything")
        self.assertEqual(delay, None)

    def test_no_delay_PUT(self):
        """
        Simple test to ensure no delay on a single call for a known limit.
        """
        delay = self.limiter.check_for_delay("PUT", "/anything")
        self.assertEqual(delay, None)

    def test_delay_PUT(self):
        """
        Ensure the 11th PUT will result in a delay of 6.0 seconds until
        the next request will be granced.
        """
        expected = [None] * 10 + [6.0]
        results = list(self._check(11, "PUT", "/anything"))

        self.assertEqual(expected, results)

    def test_delay_POST(self):
        """
        Ensure the 8th POST will result in a delay of 6.0 seconds until
        the next request will be granced.
        """
        expected = [None] * 7
        results = list(self._check(7, "POST", "/anything"))
        self.assertEqual(expected, results)

        expected = 60.0 / 7.0
        results = self._check_sum(1, "POST", "/anything")
        self.failUnlessAlmostEqual(expected, results, 8)

    def test_delay_GET(self):
        """
        Ensure the 11th GET will result in NO delay.
        """
        expected = [None] * 11
        results = list(self._check(11, "GET", "/anything"))

        self.assertEqual(expected, results)

    def test_delay_PUT_servers(self):
        """
        Ensure PUT on /servers limits at 5 requests, and PUT elsewhere is still
        OK after 5 requests...but then after 11 total requests, PUT limiting
        kicks in.
        """
        # First 6 requests on PUT /servers
        expected = [None] * 5 + [12.0]
        results = list(self._check(6, "PUT", "/servers"))
        self.assertEqual(expected, results)

        # Next 5 request on PUT /anything
        expected = [None] * 4 + [6.0]
        results = list(self._check(5, "PUT", "/anything"))
        self.assertEqual(expected, results)

    def test_delay_PUT_wait(self):
        """
        Ensure after hitting the limit and then waiting for the correct
        amount of time, the limit will be lifted.
        """
        expected = [None] * 10 + [6.0]
        results = list(self._check(11, "PUT", "/anything"))
        self.assertEqual(expected, results)

        # Advance time
        self.time += 6.0

        expected = [None, 6.0]
        results = list(self._check(2, "PUT", "/anything"))
        self.assertEqual(expected, results)

    def test_multiple_delays(self):
        """
        Ensure multiple requests still get a delay.
        """
        expected = [None] * 10 + [6.0] * 10
        results = list(self._check(20, "PUT", "/anything"))
        self.assertEqual(expected, results)

        self.time += 1.0

        expected = [5.0] * 10
        results = list(self._check(10, "PUT", "/anything"))
        self.assertEqual(expected, results)

    def test_multiple_users(self):
        """
        Tests involving multiple users.
        """
        # User1
        expected = [None] * 10 + [6.0] * 10
        results = list(self._check(20, "PUT", "/anything", "user1"))
        self.assertEqual(expected, results)

        # User2
        expected = [None] * 10 + [6.0] * 5
        results = list(self._check(15, "PUT", "/anything", "user2"))
        self.assertEqual(expected, results)

        self.time += 1.0

        # User1 again
        expected = [5.0] * 10
        results = list(self._check(10, "PUT", "/anything", "user1"))
        self.assertEqual(expected, results)

        self.time += 1.0

        # User1 again
        expected = [4.0] * 5
        results = list(self._check(5, "PUT", "/anything", "user2"))
        self.assertEqual(expected, results)


class WsgiLimiterTest(test.TestCase):
    """
    Tests for `limits.WsgiLimiter` class.
    """

    def setUp(self):
        """Run before each test."""
        test.TestCase.setUp(self)
        self.time = 0.0
        self.app = limits.WsgiLimiter(TEST_LIMITS)
        self.app._limiter._get_time = self._get_time

    def _get_time(self):
        """Return the "time" according to this test suite."""
        return self.time

    def _request_data(self, verb, path):
        """Get data decribing a limit request verb/path."""
        return json.dumps({"verb": verb, "path": path})

    def _request(self, verb, url, username=None):
        """Make sure that POSTing to the given url causes the given username
        to perform the given action.  Make the internal rate limiter return
        delay and make sure that the WSGI app returns the correct response.
        """
        if username:
            request = webob.Request.blank("/%s" % username)
        else:
            request = webob.Request.blank("/")

        request.method = "POST"
        request.body = self._request_data(verb, url)
        response = request.get_response(self.app)

        if "X-Wait-Seconds" in response.headers:
            self.assertEqual(response.status_int, 403)
            return response.headers["X-Wait-Seconds"]

        self.assertEqual(response.status_int, 204)

    def test_invalid_methods(self):
        """Only POSTs should work."""
        requests = []
        for method in ["GET", "PUT", "DELETE", "HEAD", "OPTIONS"]:
            request = webob.Request.blank("/")
            request.body = self._request_data("GET", "/something")
            response = request.get_response(self.app)
            self.assertEqual(response.status_int, 405)

    def test_good_url(self):
        delay = self._request("GET", "/something")
        self.assertEqual(delay, None)

    def test_escaping(self):
        delay = self._request("GET", "/something/jump%20up")
        self.assertEqual(delay, None)

    def test_response_to_delays(self):
        delay = self._request("GET", "/delayed")
        self.assertEqual(delay, None)

        delay = self._request("GET", "/delayed")
        self.assertEqual(delay, '60.00')

    def test_response_to_delays_usernames(self):
        delay = self._request("GET", "/delayed", "user1")
        self.assertEqual(delay, None)

        delay = self._request("GET", "/delayed", "user2")
        self.assertEqual(delay, None)

        delay = self._request("GET", "/delayed", "user1")
        self.assertEqual(delay, '60.00')

        delay = self._request("GET", "/delayed", "user2")
        self.assertEqual(delay, '60.00')


class FakeHttplibSocket(object):
    """
    Fake `httplib.HTTPResponse` replacement.
    """

    def __init__(self, response_string):
        """Initialize new `FakeHttplibSocket`."""
        self._buffer = StringIO.StringIO(response_string)

    def makefile(self, _mode, _other):
        """Returns the socket's internal buffer."""
        return self._buffer


class FakeHttplibConnection(object):
    """
    Fake `httplib.HTTPConnection`.
    """

    def __init__(self, app, host):
        """
        Initialize `FakeHttplibConnection`.
        """
        self.app = app
        self.host = host

    def request(self, method, path, body="", headers={}):
        """
        Requests made via this connection actually get translated and routed
        into our WSGI app, we then wait for the response and turn it back into
        an `httplib.HTTPResponse`.
        """
        req = webob.Request.blank(path)
        req.method = method
        req.headers = headers
        req.host = self.host
        req.body = body

        resp = str(req.get_response(self.app))
        resp = "HTTP/1.0 %s" % resp
        sock = FakeHttplibSocket(resp)
        self.http_response = httplib.HTTPResponse(sock)
        self.http_response.begin()

    def getresponse(self):
        """Return our generated response from the request."""
        return self.http_response


def wire_HTTPConnection_to_WSGI(host, app):
    """Monkeypatches HTTPConnection so that if you try to connect to host, you
    are instead routed straight to the given WSGI app.

    After calling this method, when any code calls

    httplib.HTTPConnection(host)

    the connection object will be a fake.  Its requests will be sent directly
    to the given WSGI app rather than through a socket.

    Code connecting to hosts other than host will not be affected.

    This method may be called multiple times to map different hosts to
    different apps.
    """
    class HTTPConnectionDecorator(object):
        """Wraps the real HTTPConnection class so that when you instantiate
        the class you might instead get a fake instance."""

        def __init__(self, wrapped):
            self.wrapped = wrapped

        def __call__(self, connection_host, *args, **kwargs):
            if connection_host == host:
                return FakeHttplibConnection(app, host)
            else:
                return self.wrapped(connection_host, *args, **kwargs)

    httplib.HTTPConnection = HTTPConnectionDecorator(httplib.HTTPConnection)


class WsgiLimiterProxyTest(test.TestCase):
    """
    Tests for the `limits.WsgiLimiterProxy` class.
    """

    def setUp(self):
        """
        Do some nifty HTTP/WSGI magic which allows for WSGI to be called
        directly by something like the `httplib` library.
        """
        test.TestCase.setUp(self)
        self.time = 0.0
        self.app = limits.WsgiLimiter(TEST_LIMITS)
        self.app._limiter._get_time = self._get_time
        wire_HTTPConnection_to_WSGI("169.254.0.1:80", self.app)
        self.proxy = limits.WsgiLimiterProxy("169.254.0.1:80")

    def _get_time(self):
        """Return the "time" according to this test suite."""
        return self.time

    def test_200(self):
        """Successful request test."""
        delay = self.proxy.check_for_delay("GET", "/anything")
        self.assertEqual(delay, None)

    def test_403(self):
        """Forbidden request test."""
        delay = self.proxy.check_for_delay("GET", "/delayed")
        self.assertEqual(delay, None)

        delay = self.proxy.check_for_delay("GET", "/delayed")
        self.assertEqual(delay, '60.00')
