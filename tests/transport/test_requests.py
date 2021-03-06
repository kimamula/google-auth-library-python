# Copyright 2016 Google Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import datetime
import functools

import freezegun
import mock
import pytest
import requests
import requests.adapters
from six.moves import http_client

import google.auth.credentials
import google.auth.transport.requests
from tests.transport import compliance


@pytest.fixture
def frozen_time():
    with freezegun.freeze_time("1970-01-01 00:00:00", tick=False) as frozen:
        yield frozen


class TestRequestResponse(compliance.RequestResponseTests):
    def make_request(self):
        return google.auth.transport.requests.Request()

    def test_timeout(self):
        http = mock.create_autospec(requests.Session, instance=True)
        request = google.auth.transport.requests.Request(http)
        request(url="http://example.com", method="GET", timeout=5)

        assert http.request.call_args[1]["timeout"] == 5


class TestTimeoutGuard(object):
    def make_guard(self, *args, **kwargs):
        return google.auth.transport.requests.TimeoutGuard(*args, **kwargs)

    def test_tracks_elapsed_time_w_numeric_timeout(self, frozen_time):
        with self.make_guard(timeout=10) as guard:
            frozen_time.tick(delta=3.8)
        assert guard.remaining_timeout == 6.2

    def test_tracks_elapsed_time_w_tuple_timeout(self, frozen_time):
        with self.make_guard(timeout=(16, 19)) as guard:
            frozen_time.tick(delta=3.8)
        assert guard.remaining_timeout == (12.2, 15.2)

    def test_noop_if_no_timeout(self, frozen_time):
        with self.make_guard(timeout=None) as guard:
            frozen_time.tick(delta=datetime.timedelta(days=3650))
        # NOTE: no timeout error raised, despite years have passed
        assert guard.remaining_timeout is None

    def test_timeout_error_w_numeric_timeout(self, frozen_time):
        with pytest.raises(requests.exceptions.Timeout):
            with self.make_guard(timeout=10) as guard:
                frozen_time.tick(delta=10.001)
        assert guard.remaining_timeout == pytest.approx(-0.001)

    def test_timeout_error_w_tuple_timeout(self, frozen_time):
        with pytest.raises(requests.exceptions.Timeout):
            with self.make_guard(timeout=(11, 10)) as guard:
                frozen_time.tick(delta=10.001)
        assert guard.remaining_timeout == pytest.approx((0.999, -0.001))

    def test_custom_timeout_error_type(self, frozen_time):
        class FooError(Exception):
            pass

        with pytest.raises(FooError):
            with self.make_guard(timeout=1, timeout_error_type=FooError):
                frozen_time.tick(2)

    def test_lets_suite_errors_bubble_up(self, frozen_time):
        with pytest.raises(IndexError):
            with self.make_guard(timeout=1):
                [1, 2, 3][3]


class CredentialsStub(google.auth.credentials.Credentials):
    def __init__(self, token="token"):
        super(CredentialsStub, self).__init__()
        self.token = token

    def apply(self, headers, token=None):
        headers["authorization"] = self.token

    def before_request(self, request, method, url, headers):
        self.apply(headers)

    def refresh(self, request):
        self.token += "1"


class TimeTickCredentialsStub(CredentialsStub):
    """Credentials that spend some (mocked) time when refreshing a token."""

    def __init__(self, time_tick, token="token"):
        self._time_tick = time_tick
        super(TimeTickCredentialsStub, self).__init__(token=token)

    def refresh(self, request):
        self._time_tick()
        super(TimeTickCredentialsStub, self).refresh(requests)


class AdapterStub(requests.adapters.BaseAdapter):
    def __init__(self, responses, headers=None):
        super(AdapterStub, self).__init__()
        self.responses = responses
        self.requests = []
        self.headers = headers or {}

    def send(self, request, **kwargs):
        # pylint: disable=arguments-differ
        # request is the only required argument here and the only argument
        # we care about.
        self.requests.append(request)
        return self.responses.pop(0)

    def close(self):  # pragma: NO COVER
        # pylint wants this to be here because it's abstract in the base
        # class, but requests never actually calls it.
        return


class TimeTickAdapterStub(AdapterStub):
    """Adapter that spends some (mocked) time when making a request."""

    def __init__(self, time_tick, responses, headers=None):
        self._time_tick = time_tick
        super(TimeTickAdapterStub, self).__init__(responses, headers=headers)

    def send(self, request, **kwargs):
        self._time_tick()
        return super(TimeTickAdapterStub, self).send(request, **kwargs)


def make_response(status=http_client.OK, data=None):
    response = requests.Response()
    response.status_code = status
    response._content = data
    return response


class TestAuthorizedHttp(object):
    TEST_URL = "http://example.com/"

    def test_constructor(self):
        authed_session = google.auth.transport.requests.AuthorizedSession(
            mock.sentinel.credentials
        )

        assert authed_session.credentials == mock.sentinel.credentials

    def test_constructor_with_auth_request(self):
        http = mock.create_autospec(requests.Session)
        auth_request = google.auth.transport.requests.Request(http)

        authed_session = google.auth.transport.requests.AuthorizedSession(
            mock.sentinel.credentials, auth_request=auth_request
        )

        assert authed_session._auth_request == auth_request

    def test_request_default_timeout(self):
        credentials = mock.Mock(wraps=CredentialsStub())
        response = make_response()
        adapter = AdapterStub([response])

        authed_session = google.auth.transport.requests.AuthorizedSession(credentials)
        authed_session.mount(self.TEST_URL, adapter)

        patcher = mock.patch("google.auth.transport.requests.requests.Session.request")
        with patcher as patched_request:
            authed_session.request("GET", self.TEST_URL)

        expected_timeout = google.auth.transport.requests._DEFAULT_TIMEOUT
        assert patched_request.call_args.kwargs.get("timeout") == expected_timeout

    def test_request_no_refresh(self):
        credentials = mock.Mock(wraps=CredentialsStub())
        response = make_response()
        adapter = AdapterStub([response])

        authed_session = google.auth.transport.requests.AuthorizedSession(credentials)
        authed_session.mount(self.TEST_URL, adapter)

        result = authed_session.request("GET", self.TEST_URL)

        assert response == result
        assert credentials.before_request.called
        assert not credentials.refresh.called
        assert len(adapter.requests) == 1
        assert adapter.requests[0].url == self.TEST_URL
        assert adapter.requests[0].headers["authorization"] == "token"

    def test_request_refresh(self):
        credentials = mock.Mock(wraps=CredentialsStub())
        final_response = make_response(status=http_client.OK)
        # First request will 401, second request will succeed.
        adapter = AdapterStub(
            [make_response(status=http_client.UNAUTHORIZED), final_response]
        )

        authed_session = google.auth.transport.requests.AuthorizedSession(
            credentials, refresh_timeout=60
        )
        authed_session.mount(self.TEST_URL, adapter)

        result = authed_session.request("GET", self.TEST_URL)

        assert result == final_response
        assert credentials.before_request.call_count == 2
        assert credentials.refresh.called
        assert len(adapter.requests) == 2

        assert adapter.requests[0].url == self.TEST_URL
        assert adapter.requests[0].headers["authorization"] == "token"

        assert adapter.requests[1].url == self.TEST_URL
        assert adapter.requests[1].headers["authorization"] == "token1"

    def test_request_max_allowed_time_timeout_error(self, frozen_time):
        tick_one_second = functools.partial(frozen_time.tick, delta=1.0)

        credentials = mock.Mock(
            wraps=TimeTickCredentialsStub(time_tick=tick_one_second)
        )
        adapter = TimeTickAdapterStub(
            time_tick=tick_one_second, responses=[make_response(status=http_client.OK)]
        )

        authed_session = google.auth.transport.requests.AuthorizedSession(credentials)
        authed_session.mount(self.TEST_URL, adapter)

        # Because a request takes a full mocked second, max_allowed_time shorter
        # than that will cause a timeout error.
        with pytest.raises(requests.exceptions.Timeout):
            authed_session.request("GET", self.TEST_URL, max_allowed_time=0.9)

    def test_request_max_allowed_time_w_transport_timeout_no_error(self, frozen_time):
        tick_one_second = functools.partial(frozen_time.tick, delta=1.0)

        credentials = mock.Mock(
            wraps=TimeTickCredentialsStub(time_tick=tick_one_second)
        )
        adapter = TimeTickAdapterStub(
            time_tick=tick_one_second,
            responses=[
                make_response(status=http_client.UNAUTHORIZED),
                make_response(status=http_client.OK),
            ],
        )

        authed_session = google.auth.transport.requests.AuthorizedSession(credentials)
        authed_session.mount(self.TEST_URL, adapter)

        # A short configured transport timeout does not affect max_allowed_time.
        # The latter is not adjusted to it and is only concerned with the actual
        # execution time. The call below should thus not raise a timeout error.
        authed_session.request("GET", self.TEST_URL, timeout=0.5, max_allowed_time=3.1)

    def test_request_max_allowed_time_w_refresh_timeout_no_error(self, frozen_time):
        tick_one_second = functools.partial(frozen_time.tick, delta=1.0)

        credentials = mock.Mock(
            wraps=TimeTickCredentialsStub(time_tick=tick_one_second)
        )
        adapter = TimeTickAdapterStub(
            time_tick=tick_one_second,
            responses=[
                make_response(status=http_client.UNAUTHORIZED),
                make_response(status=http_client.OK),
            ],
        )

        authed_session = google.auth.transport.requests.AuthorizedSession(
            credentials, refresh_timeout=1.1
        )
        authed_session.mount(self.TEST_URL, adapter)

        # A short configured refresh timeout does not affect max_allowed_time.
        # The latter is not adjusted to it and is only concerned with the actual
        # execution time. The call below should thus not raise a timeout error
        # (and `timeout` does not come into play either, as it's very long).
        authed_session.request("GET", self.TEST_URL, timeout=60, max_allowed_time=3.1)

    def test_request_timeout_w_refresh_timeout_timeout_error(self, frozen_time):
        tick_one_second = functools.partial(frozen_time.tick, delta=1.0)

        credentials = mock.Mock(
            wraps=TimeTickCredentialsStub(time_tick=tick_one_second)
        )
        adapter = TimeTickAdapterStub(
            time_tick=tick_one_second,
            responses=[
                make_response(status=http_client.UNAUTHORIZED),
                make_response(status=http_client.OK),
            ],
        )

        authed_session = google.auth.transport.requests.AuthorizedSession(
            credentials, refresh_timeout=100
        )
        authed_session.mount(self.TEST_URL, adapter)

        # An UNAUTHORIZED response triggers a refresh (an extra request), thus
        # the final request that otherwise succeeds results in a timeout error
        # (all three requests together last 3 mocked seconds).
        with pytest.raises(requests.exceptions.Timeout):
            authed_session.request(
                "GET", self.TEST_URL, timeout=60, max_allowed_time=2.9
            )
