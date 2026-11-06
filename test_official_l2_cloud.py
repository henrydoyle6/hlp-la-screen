"""Offline transport tests. All STS/S3 clients are explicit in-memory fakes."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest

CLOUD_TOOLS = Path(__file__).resolve().parent / 'tools' / 'official_l2'
_prior_path = list(sys.path)
try:
    sys.path.insert(0, str(CLOUD_TOOLS))
    from botocore.exceptions import ClientError
    import cloud_worker as worker
finally:
    sys.path[:] = _prior_path
assert Path(worker.__file__).resolve() == CLOUD_TOOLS / 'cloud_worker.py'
assert Path(worker.account_guard.__file__).resolve() == CLOUD_TOOLS / 'aws_account_guard.py'

KEY = 'market_data/20250501/10/l2Book/BTC.lz4'
ENDPOINT = 'https://hyperliquid-archive.s3.us-east-1.amazonaws.com/'


class Body:
    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False

    def iter_chunks(self, size):
        yield from self.chunks

    def close(self):
        self.closed = True


class FakeSTS:
    def __init__(self):
        self.calls = 0

    def get_caller_identity(self):
        self.calls += 1
        return {'Account': '555066174629',
                'Arn': 'arn:aws:sts::555066174629:assumed-role/sample-role/i-offline'}

    def close(self):
        pass


class FakeS3:
    def __init__(self, objects, response=None, list_error=None, head_error=None, head_response=None):
        self.objects, self.response, self.list_error = objects, response, list_error
        self.head_error, self.head_response = head_error, head_response
        self.list_calls, self.get_calls, self.head_calls = [], [], []
        self.meta = SimpleNamespace(events=self)
        self.hook = None

    def register(self, event, hook):
        self.hook = hook

    def list_objects_v2(self, **params):
        self.list_calls.append(params)
        self.hook(SimpleNamespace(url=ENDPOINT + '?list-type=2'))
        if self.list_error:
            raise self.list_error
        return {'Contents': self.objects, 'IsTruncated': False}

    def get_object(self, **params):
        self.get_calls.append(params)
        self.hook(SimpleNamespace(url=ENDPOINT + KEY))
        return self.response

    def head_bucket(self, **params):
        self.head_calls.append(params)
        self.hook(SimpleNamespace(url=ENDPOINT))
        if self.head_error:
            raise self.head_error
        return self.head_response or {'ResponseMetadata': {'HTTPStatusCode': 200,
                                                          'HTTPHeaders': {'x-amz-bucket-region': 'us-east-1'}}}

    def close(self):
        pass


class FakeSession:
    def __init__(self, s3):
        self.s3 = s3
        self.sts = FakeSTS()
        self.client_calls = []

    def client(self, service, **kwargs):
        self.client_calls.append((service, kwargs))
        return {'s3': self.s3, 'sts': self.sts}[service]


class ApprovedTransportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.output = Path(self.temporary.name)
        self.plan = {
            'allowed_keys': [KEY],
            'expires_at_utc': (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            'limits': {'source_requests': 8, 'source_bytes': 4096,
                       'object_bytes': 2048, 'data_runtime_seconds': 60},
        }
        self.source = None

    def tearDown(self):
        if self.source is not None:
            self.source.close()
        self.temporary.cleanup()

    def open_source(self, fake):
        session = FakeSession(fake)
        self.source = worker.ApprovedS3Source(session, 'sample-role', self.plan, self.output)
        return self.source, session

    def test_listing_without_exact_key_proves_absence_without_get(self):
        fake = FakeS3([{'Key': KEY + '.neighbor', 'Size': 3, 'ETag': 'neighbor'}])
        source, session = self.open_source(fake)
        with self.assertRaises(worker.ObjectNotFound):
            with source.open(KEY):
                self.fail('An absent exact key must never produce a body')
        self.assertEqual(fake.list_calls, [{'Bucket': 'hyperliquid-archive', 'Prefix': KEY,
                                            'MaxKeys': 1, 'RequestPayer': 'requester'}])
        self.assertEqual(fake.get_calls, [])
        self.assertEqual(session.sts.calls, 1)
        self.assertEqual(source.requests, 1)

    def test_list_access_denied_stops_without_claiming_absence(self):
        denied = ClientError({'Error': {'Code': 'AccessDenied'},
                              'ResponseMetadata': {'HTTPStatusCode': 403}}, 'ListObjectsV2')
        fake = FakeS3([], list_error=denied)
        source, session = self.open_source(fake)
        with self.assertRaisesRegex(worker.SourceAccessError, 'AccessDenied'):
            with source.open(KEY):
                self.fail('A denied listing must not yield an object')
        self.assertEqual(fake.get_calls, [])
        self.assertEqual(session.sts.calls, 1)
        self.assertEqual(source.requests, 1)

    def test_changed_etag_rejects_response_and_closes_body(self):
        body = Body([b'abc'])
        fake = FakeS3([{'Key': KEY, 'Size': 3, 'ETag': 'listed-version'}],
                      {'ETag': 'different-version', 'ContentLength': 3, 'Body': body})
        source, session = self.open_source(fake)
        with self.assertRaisesRegex(worker.SourceAccessError, 'identity changed'):
            with source.open(KEY):
                self.fail('A changed source version must not reach the decoder')
        self.assertTrue(body.closed)
        self.assertEqual(fake.get_calls[0]['IfMatch'], 'listed-version')
        self.assertEqual(session.sts.calls, 2)
        self.assertEqual(source.reserved, 0)
        self.assertEqual(source.received, 0)

    def test_listed_version_is_streamed_and_accounted_exactly(self):
        body = Body([b'ab', b'cdef', b'g'])
        fake = FakeS3([{'Key': KEY, 'Size': 7, 'ETag': 'same-version'}],
                      {'ETag': 'same-version', 'ContentLength': 7, 'Body': body})
        source, session = self.open_source(fake)
        with source.open(KEY) as opened:
            self.assertEqual(opened.identity['region'], 'us-east-1')
            self.assertEqual(opened.identity['etag'], 'same-version')
            self.assertEqual(b''.join(opened.iter_chunks()), b'abcdefg')
        self.assertTrue(body.closed)
        self.assertEqual(session.sts.calls, 2)
        self.assertEqual(source.requests, 2)
        self.assertEqual((source.reserved, source.received), (7, 7))
        self.assertEqual(fake.get_calls, [{'Bucket': 'hyperliquid-archive', 'Key': KEY,
                                           'RequestPayer': 'requester', 'IfMatch': 'same-version'}])
        self.assertEqual(source.database.execute('SELECT kind,bytes_reserved FROM aws_usage ORDER BY sequence').fetchall(),
                         [('ListObjectsV2', 0), ('GetObject', 7)])

    def test_sdk_generates_only_regional_bucket_and_object_endpoints(self):
        fake = FakeS3([])
        source, session = self.open_source(fake)
        arguments = next(kwargs for service, kwargs in session.client_calls if service == 's3')
        self.assertEqual(arguments['region_name'], 'us-east-1')
        self.assertEqual(arguments['config'].s3['us_east_1_regional_endpoint'], 'regional')
        client = worker.boto3.Session(aws_access_key_id='OFFLINE_TEST_KEY',
                                      aws_secret_access_key='OFFLINE_TEST_SECRET').client('s3', **arguments)
        captured = []

        class RequestCaptured(Exception):
            pass

        def intercept(request, **kwargs):
            captured.append(request.url)
            raise RequestCaptured()

        client.meta.events.register('before-send.s3.*', intercept)
        try:
            with self.assertRaises(RequestCaptured):
                client.head_bucket(Bucket='hyperliquid-archive')
            with self.assertRaises(RequestCaptured):
                client.get_object(Bucket='hyperliquid-archive', Key=KEY, RequestPayer='requester')
        finally:
            client.close()
        self.assertEqual(len(captured), 2)
        bucket_url, object_url = map(worker.urllib.parse.urlsplit, captured)
        self.assertEqual(bucket_url.hostname, 'hyperliquid-archive.s3.us-east-1.amazonaws.com')
        self.assertEqual(bucket_url.path, '/')
        self.assertEqual(object_url.hostname, 'hyperliquid-archive.s3.us-east-1.amazonaws.com')
        self.assertEqual(object_url.path, '/' + KEY)

    def test_wire_guard_rejects_cross_region_redirect_before_counting_request(self):
        source, session = self.open_source(FakeS3([]))
        for operation, hostname in [
            ('HeadBucket', 'hyperliquid-archive.s3.ap-northeast-1.amazonaws.com'),
            ('GetObject', 'hyperliquid-archive.s3.ap-northeast-1.amazonaws.com'),
        ]:
            with self.subTest(operation=operation):
                source.current_operation = operation
                source.current_key = KEY
                source.verify_identity()
                with self.assertRaisesRegex(worker.SourceAccessError, 'cross-region'):
                    source.wire_request(SimpleNamespace(url='https://' + hostname + '/' + KEY))
        self.assertEqual(source.requests, 0)
        self.assertEqual(source.database.execute('SELECT count(*) FROM aws_usage').fetchone()[0], 0)

    def test_head_bucket_403_with_us_east_1_header_verifies_region_without_reading_body(self):
        denied = ClientError({'Error': {'Code': '403'}, 'ResponseMetadata': {
            'HTTPStatusCode': 403, 'HTTPHeaders': {'x-amz-bucket-region': 'us-east-1'}}}, 'HeadBucket')
        fake = FakeS3([], head_error=denied)
        source, session = self.open_source(fake)
        source.verify_bucket_region()
        self.assertEqual(fake.head_calls, [{'Bucket': 'hyperliquid-archive'}])
        self.assertEqual(fake.list_calls, [])
        self.assertEqual(fake.get_calls, [])
        self.assertEqual((source.requests, source.received, source.reserved), (1, 0, 0))
        self.assertEqual(session.sts.calls, 1)
        self.assertEqual(source.database.execute('SELECT kind FROM aws_usage').fetchall(), [('HeadBucket',)])

    def test_head_bucket_403_without_region_header_aborts(self):
        denied = ClientError({'Error': {'Code': '403'}, 'ResponseMetadata': {
            'HTTPStatusCode': 403, 'HTTPHeaders': {}}}, 'HeadBucket')
        fake = FakeS3([], head_error=denied)
        source, _session = self.open_source(fake)
        with self.assertRaisesRegex(worker.SourceAccessError, 'region missing'):
            source.verify_bucket_region()
        self.assertEqual(fake.get_calls, [])
        self.assertEqual(source.received, 0)

    def test_head_bucket_wrong_region_aborts_even_with_a_readable_region_header(self):
        denied = ClientError({'Error': {'Code': '403'}, 'ResponseMetadata': {
            'HTTPStatusCode': 403, 'HTTPHeaders': {'x-amz-bucket-region': 'ap-northeast-1'}}}, 'HeadBucket')
        fake = FakeS3([], head_error=denied)
        source, _session = self.open_source(fake)
        with self.assertRaisesRegex(worker.SourceAccessError, 'outside us-east-1'):
            source.verify_bucket_region()
        self.assertEqual(fake.get_calls, [])
        self.assertEqual(source.received, 0)


if __name__ == '__main__':
    unittest.main()
