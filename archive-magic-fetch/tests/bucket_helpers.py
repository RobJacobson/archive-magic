"""In-memory S3 boundary with conditional reads and opaque multipart ETags."""
from io import BytesIO
from pathlib import Path


class Bucket:
    def __init__(self):
        self.objects = {}
        self.versions = {}
        self.uploads = []
        self.fail = None

    def put(self, key, body):
        self.objects[key] = body
        self.versions[key] = self.versions.get(key, 0) + 1

    def signature(self, key):
        return f'"opaque-{self.versions[key]}-2"'

    def get_paginator(self, name):
        assert name == 'list_objects_v2'
        return self

    def paginate(self, *, Bucket, Prefix):
        yield {'Contents': [{'Key': k, 'ETag': self.signature(k), 'Size': len(v), 'LastModified': str(self.versions[k])} for k, v in sorted(self.objects.items()) if k.startswith(Prefix)]}

    def get_object(self, *, Bucket, Key, IfMatch):
        assert self.signature(Key) == IfMatch
        return {'Body': BytesIO(self.objects[Key])}

    def upload_file(self, path, bucket, key):
        if self.fail and self.fail(key):
            raise OSError('simulated upload outage')
        self.uploads.append(key)
        self.put(key, Path(path).read_bytes())

    def put_object(self, *, Bucket, Key, Body, **kwargs):
        self.uploads.append(Key)
        self.put(Key, Body)
