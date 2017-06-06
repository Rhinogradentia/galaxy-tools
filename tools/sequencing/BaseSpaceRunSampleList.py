

from urllib2 import Request, urlopen, URLError
from os.path import expanduser
import re
import hashlib
import json
import sys
import os
import logging
logging.basicConfig(level=logging.DEBUG)
log = logging.getLogger()


CHUNK_SIZE = 128 * 1024
RUN_ID = sys.argv[1]


with open('/galaxy/creds.json', 'r') as handle:
    data = json.load(handle)
    AccessToken = data['BaseSpace']

API_BASE = 'https://api.basespace.illumina.com/'
ACCESS_TOKEN = '?access_token=%s' % AccessToken


def restrequest(rawrequest):
    rawrequest = API_BASE + rawrequest + ACCESS_TOKEN
    log.debug('Req: ' + rawrequest.replace(AccessToken, '*' * len(AccessToken)))

    req_url_hash = hashlib.md5(rawrequest).hexdigest()
    cache_path_basedir = os.path.join(expanduser("~"), '.cache', 'basespace')
    if not os.path.exists(cache_path_basedir):
        os.makedirs(cache_path_basedir)

    cache_path = os.path.join(cache_path_basedir, req_url_hash)
    if os.path.exists(cache_path):
        with open(cache_path, 'r') as handle:
            json_obj = json.load(handle)
            return json_obj

    log.debug("Cache miss")
    request = Request(rawrequest)

    try:
        response = urlopen(request)
        json_string = response.read()
        json_obj = json.loads(json_string)
    except URLError as e:
        print('Got an error code:', e)
        sys.exit()

    with open(cache_path, 'w') as handle:
        json.dump(json_obj, handle)

    return json_obj


json_obj = restrequest('v1pre3/runs/%s' % RUN_ID)

for item in json_obj['Response']['Properties']['Items']:
    cols = ['Id', 'TotalReadsRaw', 'Name', 'SampleId']
    for item2 in item['Items']:
        data = map(str, [item2[y] for y in cols])
        print('\t'.join(data))
