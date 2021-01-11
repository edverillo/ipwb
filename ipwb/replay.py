#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
InterPlanetary Wayback Replay system

This script handles requests to replay IPWB archived contents based on a
supplied CDXJ file. This file has been previously generated by the ipwb
indexer. An interface is supplied when first started to assist the user in
navigating their captures.
"""

import sys
import os
import ipfshttpclient as ipfsapi
import json
import subprocess
import pkg_resources
import surt
import re
import traceback
import tempfile

from flask import (
    Flask, Response, request, redirect, render_template,
)

from bisect import bisect_left
from socket import gaierror
from socket import error as socketerror

from six.moves.urllib_parse import urlsplit, urlunsplit


from requests.exceptions import HTTPError

from . import util as ipwb_utils
from .backends import get_web_archive_index
from .exceptions import IPFSDaemonNotAvailable
from .util import unsurt, ipfs_client
from .util import IPWBREPLAY_HOST, IPWBREPLAY_PORT
from .util import INDEX_FILE

from . import indexer

from base64 import b64decode
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

import base64

from werkzeug.routing import BaseConverter
from .__init__ import __version__ as ipwb_version


from flask import flash
from werkzeug.utils import secure_filename
from flask import send_from_directory
from flask import make_response

import logging

logger = logging.getLogger(__name__)

UPLOAD_FOLDER = tempfile.gettempdir()
ALLOWED_EXTENSIONS = ('.warc', '.warc.gz')

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.debug = False


@app.context_processor
def formatters():
    return {'pluralize': lambda x, s, p: "{} {}".format(x, s if x == 1 else p)}


@app.after_request
def set_server_header(response):
    response.headers['Server'] = ('InterPlanetary Wayback Replay/'
                                  f'{ipwb_version}')
    response.autocorrect_location_header = False
    return response


def allowed_file(filename):
    return filename.lower().endswith(ALLOWED_EXTENSIONS)


@app.route('/upload', methods=['POST'])
def upload_file():
    # check if the post request has the file part
    resp = redirect(request.url)

    if 'file' not in request.files:
        flash('No file part')
        return resp

    file = request.files['file']
    # if user does not select file, browser also
    # submit an empty part without filename
    if file.filename == '':
        flash('No selected file')
        return resp
    if file and allowed_file(file.filename):
        filename = secure_filename(file.filename)
        warc_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        file.save(warc_path)

        # TODO: Check if semaphore lock exists, log it if so, wait for the lock
        # to be released, and create a new lock

        print((f'Indexing file from uploaded WARC at'
               f'{warc_path} to {app.cdxj_file_path}'))
        indexer.index_file_at(warc_path, outfile=app.cdxj_file_path)
        print(f'Index updated at {app.cdxj_file_path}')

        # TODO: Release semaphore lock
        resp.location = request.referrer

        return resp


@app.route('/ipwbassets/<path:path>')
def serve_assets(path):
    resp = make_response(send_from_directory('assets', path))
    if path == 'serviceWorker.js':
        resp.headers['Service-Worker-Allowed'] = '/'
    return resp


class UnsupportedIPFSVersions(Exception):
    pass


@app.route('/ipfsdaemon/<cmd>')
def command_daemon(cmd):
    if cmd == 'status':
        return generate_daemon_status_button()
    elif cmd == 'start':
        subprocess.Popen(['ipfs', 'daemon'])
        return Response('IPFS daemon starting...')

    elif cmd == 'stop':
        try:
            ipfs_version = ipfs_client().version()['Version']
            if ipwb_utils.compare_versions(ipfs_version, '0.4.10') < 0:
                raise UnsupportedIPFSVersions()
            ipfs_client().shutdown()
        except (subprocess.CalledProcessError, UnsupportedIPFSVersions) as e:
            if os.name != 'nt':  # Big hammer
                subprocess.call(['killall', 'ipfs'])
            else:
                subprocess.call(['taskkill', '/im', 'ipfs.exe', '/F'])

        return Response('IPFS daemon stopping...')
    elif cmd == 'webuilink':
        return Response(ipwb_utils.get_ipfsapi_host_and_port() + '/webui')
    else:
        print('ERROR, bad command sent to daemon API!')
        print(cmd)
        return Response('bad command!')


@app.route('/memento/*/')
def show_mementos_for_urirs_sans_js():
    urir = request.args.get('url')
    if urir is None or urir.strip() == '':
        return Response('Searching for nothing is not allowed!', status=400)

    return redirect(f'/memento/*/{urir}', code=301)


@app.route('/memento/*/<path:urir>')
def show_mementos_for_urirs(urir):
    urir = compile_target_uri(urir, request.query_string)

    if ipwb_utils.is_localhosty(urir):
        urir = urir.split('/', 4)[4]

    index_path = ipwb_utils.get_ipwb_replay_index_path()

    print(f'Getting CDXJ lines with the URI-R {urir} from {index_path}')
    cdxj_lines_with_urir = get_cdxj_lines_with_urir(urir, index_path)

    if len(cdxj_lines_with_urir) == 1:
        fields = cdxj_lines_with_urir[0].split(' ', 2)
        redirect_uri = f'/memento/{fields[1]}/{unsurt(fields[0])}'

        return redirect(redirect_uri, code=302)

    msg = ''
    if cdxj_lines_with_urir:
        msg += f'<p>{len(cdxj_lines_with_urir)} capture(s) available:</p><ul>'

        for line in cdxj_lines_with_urir:
            fields = line.split(' ', 2)
            dt14 = fields[1]
            dt_rfc1123 = ipwb_utils.digits14_to_rfc1123(fields[1])
            msg += (f'<li><a href="/memento/{dt14}/{unsurt(fields[0])}">'
                    f'{unsurt(fields[0])} at {dt_rfc1123}</a></li>')
        msg += '</ul>'
    else:  # No captures for URI-R
        msg = generate_no_mementos_interface_noDatetime(urir)

    return Response(msg)


class RegexConverter(BaseConverter):
    def __init__(self, url_map, *items):
        super(RegexConverter, self).__init__(url_map)
        self.regex = items[0]


app.url_map.converters['regex'] = RegexConverter


def resolve_memento(urir, datetime):
    """ Request a URI-R at a supplied datetime from the CDXJ """
    if ipwb_utils.is_localhosty(urir):
        urir = urir.split('/', 4)[4]
    s = surt.surt(urir, path_strip_trailing_slash_unless_empty=False)
    index_path = ipwb_utils.get_ipwb_replay_index_path()

    print(f'Getting CDXJ lines with the URI-R {urir} from {index_path}')
    cdxj_lines_with_urir = get_cdxj_lines_with_urir(urir, index_path)

    closest_line = get_cdxj_line_closest_to(datetime, cdxj_lines_with_urir)

    if closest_line is None:
        msg = '<h1>ERROR 404</h1>'
        msg += f'<p>No captures found for {urir} at {datetime}.</p>'

        return Response(msg, status=404)

    uri = unsurt(closest_line.split(' ')[0])
    new_datetime = closest_line.split(' ')[1]

    link_header = get_link_header_abbreviated_timemap(urir, new_datetime)

    return (new_datetime, link_header, uri)


def compile_target_uri(url: str, query_string: bytes) -> str:
    """Append GET query string to the page path, to get full URI."""
    if query_string:
        return f"{url}?{query_string.decode('utf-8')}"

    else:
        return url


@app.route('/memento/<regex("[0-9]{1,14}"):datetime>/<path:urir>')
def show_memento(urir, datetime):
    urir = compile_target_uri(urir, request.query_string)

    try:
        datetime = ipwb_utils.pad_digits14(datetime, validate=True)
    except ValueError as e:
        msg = f'Expected a 4-14 digits valid datetime: {datetime}'
        return Response(msg, status=400)
    resolved_memento = resolve_memento(urir, datetime)

    # resolved to a 404, flask Response object returned instead of tuple
    if isinstance(resolved_memento, Response):
        return resolved_memento
    (new_datetime, link_header, uri) = resolved_memento

    if new_datetime != datetime:
        resp = redirect(f'/memento/{new_datetime}/{urir}', code=302)
    else:
        resp = show_uri(uri, new_datetime)

    resp.headers['Link'] = link_header

    return resp


def get_cdxj_line_closest_to(datetime_target, cdxj_lines):
    """ Get the closest CDXJ entry for a datetime and URI-R """
    smallest_diff = float('inf')  # math.inf is only py3
    best_line = None
    datetime_target = int(datetime_target)
    for cdxj_line in cdxj_lines:
        dt = int(cdxj_line.split(' ')[1])
        diff = abs(dt - datetime_target)
        if diff < smallest_diff:
            smallest_diff = diff
            best_line = cdxj_line
    return best_line


def get_cdxj_lines_with_urir(urir, index_path):
    """ Get all CDXJ records corresponding to a URI-R """
    if not index_path:
        index_path = ipwb_utils.get_ipwb_replay_index_path()

    index_path = get_index_file_full_path(index_path)

    print(f'Getting CDXJ lines with {urir} in {index_path}')
    s = surt.surt(urir, path_strip_trailing_slash_unless_empty=False)
    cdxj_lines_with_urir = []

    cdxj_line_index = get_cdxj_line_binarySearch(
        s, index_path, True, True)  # get i

    if cdxj_line_index is None:
        return []

    cdxj_lines = []

    content = get_web_archive_index(index_path)

    cdxj_lines = content.split('\n')
    base_cdxj_line = cdxj_lines[cdxj_line_index]  # via binsearch

    cdxj_lines_with_urir.append(base_cdxj_line)

    # Get lines before pivot that match surt
    sI = cdxj_line_index - 1
    while sI >= 0:
        if cdxj_lines[sI].split(' ')[0] == s:
            cdxj_lines_with_urir.append(cdxj_lines[sI])
        sI -= 1
    # Get lines after pivot that match surt
    sI = cdxj_line_index + 1
    while sI < len(cdxj_lines):
        if cdxj_lines[sI].split(' ')[0] == s:
            cdxj_lines_with_urir.append(cdxj_lines[sI])
        sI += 1
    return cdxj_lines_with_urir


@app.route('/timegate/<path:urir>')
def query_timegate(urir):
    urir = compile_target_uri(urir, request.query_string)

    adt = request.headers.get("Accept-Datetime")
    if adt is None:
        adt = ipwb_utils.get_rfc1123_of_now()

    if not ipwb_utils.is_rfc1123_compliant(adt):
        return "Bad Request", 400

    datetime14 = ipwb_utils.rfc1123_to_digits14(adt)

    resolved_memento = resolve_memento(urir, datetime14)

    if isinstance(resolved_memento, Response):
        return resolved_memento
    (new_datetime, link_header, uri) = resolved_memento

    resp = redirect(f'/memento/{new_datetime}/{urir}', code=302)

    resp.headers['Link'] = link_header
    resp.headers['Vary'] = 'Accept-Datetime'

    return resp


@app.route('/timemap/<regex("link|cdxj"):format>/<path:urir>')
def show_timemap(urir, format):
    urir = compile_target_uri(urir, request.query_string)

    s = surt.surt(urir, path_strip_trailing_slash_unless_empty=False)
    index_path = ipwb_utils.get_ipwb_replay_index_path()

    cdxj_lines_with_urir = get_cdxj_lines_with_urir(urir, index_path)
    tm_content_type = ''

    host_and_port = ipwb_utils.get_ipwb_replay_config()

    tg_uri = f'http://{host_and_port[0]}:{host_and_port[1]}/timegate/{urir}'

    tm = ''  # Initialize for usage beyond below conditionals
    if format == 'link':
        tm = generate_link_timemap_from_cdxj_lines(
            cdxj_lines_with_urir, s, request.url, tg_uri)
        tm_content_type = 'application/link-format'
    elif format == 'cdxj':
        tm = generate_cdxj_timemap_from_cdxj_lines(
            cdxj_lines_with_urir, s, request.url, tg_uri)
        tm_content_type = 'application/cdxj+ors'

    resp = Response(tm)
    resp.headers['Content-Type'] = tm_content_type

    return resp


def get_link_header_abbreviated_timemap(urir, pivot_datetime):
    s = surt.surt(urir, path_strip_trailing_slash_unless_empty=False)
    index_path = ipwb_utils.get_ipwb_replay_index_path()

    cdxj_lines_with_urir = get_cdxj_lines_with_urir(urir, index_path)
    host_and_port = ipwb_utils.get_ipwb_replay_config()

    tg_uri = f'http://{host_and_port[0]}:{host_and_port[1]}/timegate/{urir}'

    tm_uri = (f'http://{host_and_port[0]}:{host_and_port[1]}'
              f'/timemap/link/{urir}')
    tm = generate_link_timemap_from_cdxj_lines(
        cdxj_lines_with_urir, s, tm_uri, tg_uri)

    # Fix base TM relation when viewing abbrev version in Link resp
    tm = tm.replace('rel="self timemap"', 'rel="timemap"')

    # Only one memento in TimeMap
    if 'rel="first last memento"' in tm:
        return tm.replace('\n', ' ').strip()

    tm_lines = tm.split('\n')
    for idx, line in enumerate(tm_lines):
        if len(re.findall('rel=.*memento"', line)) == 0:
            continue  # Not a memento

        if pivot_datetime in line:
            add_both_next_and_prev = False
            if idx > 0 and idx < len(tm_lines) - 1:
                add_both_next_and_prev = True

            if add_both_next_and_prev or idx == 0:
                tm_lines[idx + 1] = \
                    tm_lines[idx + 1].replace('memento"', 'next memento"')
            if add_both_next_and_prev or idx == len(tm_lines) - 1:
                tm_lines[idx - 1] = \
                    tm_lines[idx - 1].replace('memento"', 'prev memento"')
            break

    # Remove all mementos in abbrev TM that are not:
    #   first, last, prev, next, or pivot
    for idx, line in enumerate(tm_lines):
        if len(re.findall('rel=.*memento"', line)) == 0:
            continue  # Not a memento
        if pivot_datetime in line:
            continue

        if len(re.findall('rel=.*(next|prev|first|last)', line)) == 0:
            tm_lines[idx] = ''

    return ' '.join(filter(None, tm_lines))


def get_proxied_urit(uriT):
    tmurl = list(urlsplit(uriT))
    if app.proxy is not None:
        # urlsplit put domain in path for "example.com"
        tmurl[1] = app.proxy  # Set replay host/port if no scheme
        proxy_uri = urlsplit(app.proxy)
        if proxy_uri.scheme != '':
            tmurl[0] = proxy_uri.scheme
            tmurl[1] = proxy_uri.netloc + proxy_uri.path

    return tmurl


def generate_link_timemap_from_cdxj_lines(
        cdxj_lines, original, tm_self, tg_uri):
    tmurl = get_proxied_urit(tm_self)

    if app.proxy is not None:
        tm_self = urlunsplit(tmurl)
        tg_uri = urlunsplit(get_proxied_urit(tg_uri))

    # Extract and trim for host:port prepending
    tmurl[2] = ''  # Clear TM path
    host_and_port = f'{urlunsplit(tmurl)}/'

    # unsurted URI will never have a scheme, add one
    original_uri = f'http://{unsurt(original)}'

    tm_data = f'<{original_uri}>; rel="original",\n'
    tm_data += f'<{tm_self}>; rel="self timemap"; '
    tm_data += 'type="application/link-format",\n'

    cdxj_tm_uri = tm_self.replace('/timemap/link/', '/timemap/cdxj/')
    tm_data += f'<{cdxj_tm_uri}>; rel="timemap"; '
    tm_data += 'type="application/cdxj+ors",\n'

    tm_data += f'<{tg_uri}>; rel="timegate"'

    for i, line in enumerate(cdxj_lines):
        (surt_uri, datetime, json) = line.split(' ', 2)
        dt_rfc1123 = ipwb_utils.digits14_to_rfc1123(datetime)
        first_last_str = ''

        if len(cdxj_lines) > 1:
            if i == 0:
                first_last_str = 'first '
            elif i == len(cdxj_lines) - 1:
                first_last_str = 'last '
        elif len(cdxj_lines) == 1:
            first_last_str = 'first last '

        tm_data += (
            f',\n<{host_and_port}memento/{datetime}/{unsurt(surt_uri)}>; '
            f'rel="{first_last_str}memento"; datetime="{dt_rfc1123}"')
    return f'{tm_data}\n'


def generate_cdxj_timemap_from_cdxj_lines(
        cdxj_lines, original, tm_self, tg_uri):
    tmurl = get_proxied_urit(tm_self)
    if app.proxy is not None:
        tm_self = urlunsplit(tmurl)
        tg_uri = urlunsplit(get_proxied_urit(tg_uri))

    # unsurted URI will never have a scheme, add one
    original_uri = f'http://{unsurt(original)}'

    tm_data = '!context ["http://tools.ietf.org/html/rfc7089"]\n'
    tm_data += f'!id {{"uri": "{tm_self}"}}\n'
    tm_data += '!keys ["memento_datetime_YYYYMMDDhhmmss"]\n'
    tm_data += f'!meta {{"original_uri": "{original_uri}"}}\n'
    tm_data += f'!meta {{"timegate_uri": "{tg_uri}"}}\n'
    link_tm_uri = tm_self.replace('/timemap/cdxj/', '/timemap/link/')
    tm_data += (f'!meta {{"timemap_uri": {{'
                f'"link_format": "{link_tm_uri}",'
                f''f'"cdxj_format": "{tm_self}"'
                f'}}}}\n')
    host_and_port = tm_self[0:tm_self.index('timemap/')]

    for i, line in enumerate(cdxj_lines):
        (surt_uri, datetime, json) = line.split(' ', 2)
        dt_rfc1123 = ipwb_utils.digits14_to_rfc1123(datetime)
        first_last_str = ''

        if len(cdxj_lines) > 1:
            if i == 0:
                first_last_str = 'first '
            elif i == len(cdxj_lines) - 1:
                first_last_str = 'last '
        elif len(cdxj_lines) == 1:
            first_last_str = 'first last '

        tm_data += (f'{datetime} {{'
                    f'"uri": "{host_and_port}memento/{datetime}/{surt_uri}", '
                    f'"rel": "{first_last_str}memento", '
                    f'"datetime"="{dt_rfc1123}"}}\n')
    return tm_data


@app.errorhandler(Exception)
def all_exception_handler(error):
    print(error)
    print(sys.exc_info())
    traceback.print_tb(sys.exc_info()[-1])

    return 'Error', 500


@app.route('/ipwbadmin', strict_slashes=False)
def show_admin():
    status = {'ipwb_version': ipwb_version,
              'ipfs_endpoint': ipwb_utils.IPFSAPI_MUTLIADDRESS}
    index_file = ipwb_utils.get_ipwb_replay_index_path()

    memento_info = calculate_memento_info_in_index(index_file)

    m_count = memento_info['memento_count']
    unique_urirs = len(memento_info['surt_uris'].keys())
    html_count = memento_info['html_count']
    oldest_datetime = memento_info['oldest_datetime']
    newest_datetime = memento_info['newest_datetime']

    uris = get_uris_and_datetimes_in_cdxj(index_file)

    # TODO: Calculate actual URI-R/M counts
    indexes = [{'path': ipwb_utils.get_ipwb_replay_index_path(),
                'enabled': True,
                'urim_count': m_count,
                'urir_count': unique_urirs}]
    # TODO: Calculate actual values
    summary = {'urim_count': m_count,
               'urir_count': unique_urirs,
               'uris': uris,
               'html_count': html_count,
               'earliest': oldest_datetime,
               'latest': newest_datetime}

    return render_template('admin.html', status=status, indexes=indexes,
                           summary=summary)


@app.route('/', strict_slashes=False)
def show_landing_page():
    index_file = ipwb_utils.get_ipwb_replay_index_path()
    memento_info = calculate_memento_info_in_index(index_file)

    m_count = memento_info['memento_count']
    unique_urirs = len(memento_info['surt_uris'].keys())
    html_count = memento_info['html_count']

    summary = {'index_path': index_file,
               'urim_count': m_count,
               'urir_count': unique_urirs,
               'html_count': html_count}
    uris = get_uris_and_datetimes_in_cdxj(index_file)
    return render_template('index.html', summary=summary, uris=uris)


def show_uri(path, datetime=None):
    try:
        ipwb_utils.check_daemon_is_alive(ipwb_utils.IPFSAPI_MUTLIADDRESS)

    except IPFSDaemonNotAvailable:
        errStr = ('IPFS daemon not running. '
                  'Start it using $ ipfs daemon on the command-line '
                  ' or from the <a href="/">'
                  'IPWB replay homepage</a>.')

        return Response(errStr, status=503)

    cdxj_line = ''
    try:
        surted_uri = surt.surt(
                     path, path_strip_trailing_slash_unless_empty=False)
        index_path = ipwb_utils.get_ipwb_replay_index_path()

        search_string = surted_uri
        if datetime is not None:
            search_string = f'{surted_uri} {datetime}'

        cdxj_line = get_cdxj_line_binarySearch(search_string, index_path)

    except Exception as e:
        print(sys.exc_info()[0])
        resp_string = (
            f'{path} not found :('
            f' <a href="http://{IPWBREPLAY_HOST}:{IPWBREPLAY_PORT}">'
            f'Go home</a>')
        return Response(resp_string)
    if cdxj_line is None:  # Resource not found in archives
        return generate_no_mementos_interface(path, datetime)

    cdxj_parts = cdxj_line.split(" ", 2)
    json_object = json.loads(cdxj_parts[2])
    datetime = cdxj_parts[1]

    digests = json_object['locator'].split('/')

    class HashNotFoundError(Exception):
        pass

    payload = None
    header = None
    try:
        def handler(signum, frame):
            raise HashNotFoundError()

        # if os.name != 'nt':  # Bug #310
        #    signal.signal(signal.SIGALRM, handler)
        #    signal.alarm(10)

        payload = ipfs_client().cat(digests[-1])
        header = ipfs_client().cat(digests[-2])

        # if os.name != 'nt':  # Bug #310
        #    signal.alarm(0)

    except ipfsapi.exceptions.TimeoutError:
        print(f"{cdxj_parts[0]} not found at {digests[-1]}")
        resp_string = (
            f'{path} not found in IPFS :('
            f' <a href="http://{IPWBREPLAY_HOST}:{IPWBREPLAY_PORT}">'
            f'Go home</a>')
        return Response(resp_string)
    except TypeError as e:
        print('A type error occurred')
        print(e)
        return "A Type Error Occurred", 500
    except HTTPError as e:
        print("Fetching from the IPFS failed")
        print(e)
        return "Fetching from IPFS failed", 503
    except HashNotFoundError:
        if payload is None:
            print(f"Hashes not found:\n\t{digests[-1]}\n\t{digests[-2]}")
            return "Hashed not found", 404
        else:  # payload found but not header, fabricate header
            print("HTTP header not found, fabricating for resp replay")
            header = ''
    except Exception as e:
        print('Unknown exception occurred while fetching from ipfs.')
        print(e)
        return "An unknown exception occurred", 500

    if 'encryption_method' in json_object:
        key_string = None
        while key_string is None:
            if 'encryption_key' in json_object:
                key_string = json_object['encryption_key']
            else:
                ask_for_key = ('Enter a path for file',
                               ' containing decryption key: \n> ')
                key_string = raw_input(ask_for_key)

        padded_encryption_key = pad(key_string, AES.block_size)
        key = base64.b64encode(padded_encryption_key)

        nonce = b64decode(json_object['encryption_nonce'])
        cipher = AES.new(key, AES.MODE_CTR, nonce=nonce)
        header = cipher.decrypt(base64.b64decode(header))
        payload = cipher.decrypt(base64.b64decode(payload))

    h_lines = header.decode() \
        .replace('\r', '') \
        .replace('\n\t', '\t') \
        .replace('\n ', ' ') \
        .split('\n')
    h_lines.pop(0)

    status = 200
    if 'status_code' in json_object:
        status = json_object['status_code']

    resp = Response(payload, status=status)

    for idx, hLine in enumerate(h_lines):
        k, v = hLine.split(':', 1)

        if k.lower() == 'transfer-encoding' and \
                re.search(r'\bchunked\b', v, re.I):
            try:
                unchunked_payload = extract_response_from_chunked_data(payload)
            except Exception as e:
                continue  # Data not chunked
            resp.set_data(unchunked_payload)

        if k.lower() not in ["content-type", "content-encoding", "location"]:
            k = f'X-Archive-Orig-{k}'

        resp.headers[k] = v.strip()

    # Add ipwb header for additional SW logic
    new_payload = resp.get_data()

    line_json = cdxj_line.split(' ', 2)[2]
    mime = json.loads(line_json)['mime_type']

    if 'text/html' in mime:
        ipwb_js_inject = """<script src="/ipwbassets/webui.js"></script>
                      <script>injectIPWBJS()</script>"""

        new_payload = new_payload.decode('utf-8').replace(
            '</html>', f'{ipwb_js_inject}</html>')

        resp.set_data(new_payload)

    resp.headers['Memento-Datetime'] = ipwb_utils.digits14_to_rfc1123(datetime)

    if header is None:
        resp.headers['X-Headers-Generated-By'] = 'InterPlanetary Wayback'

    # Get TimeMap for Link response header
    # respWithlink_header = get_link_header_abbreviated_timemap(path, datetime)
    # resp.headers['Link'] = respWithlink_header.replace('\n', ' ')

    if status[0] == '3' and isUri(resp.headers.get('Location')):
        # Bad assumption that the URI-M will contain \d14 but works for now.
        uri_before_urir = request.url[
                          :re.search(r'/\d{14}/', request.url).end()]
        new_urim = uri_before_urir + resp.headers['Location']
        resp.headers['Location'] = new_urim

    return resp


def isUri(str):
    return re.match('^https?://', str, flags=re.IGNORECASE)


def generate_no_mementos_interface_noDatetime(urir):
    msg = '<h1>ERROR 404</h1>'
    msg += f'<p>No captures found for {urir}.</p>'

    msg += (f'<form method="get" action="/memento/*/" '
            f'style="margin-top: 1.0em;">'
            f'<input type="text" value="{urir}" id="url" '
            f'name="url" aria-label="Enter a URI" required />'
            f'<input type="submit" value="Search URL in the archive"/>'
            f'</form>')

    return msg


@app.errorhandler(404)
def page_not_found(e):
    return "<h1>ERROR 404</h1><p>Resource not found</p>", 404


def generate_no_mementos_interface(path, datetime):
    msg = '<h1>ERROR 404</h1>'
    msg += f'<p>No captures found for {path} at {datetime}.</p>'

    lines_with_same_urir = get_cdxj_lines_with_urir(path, None)
    print(f'CDXJ lines with URI-R at {path}')
    print(lines_with_same_urir)

    # TODO: Use closest instead of conditioning on single entry
    #  temporary fix for core functionality in #225
    if len(lines_with_same_urir) == 1:
        fields = lines_with_same_urir[0].split(' ', 2)
        redirect_uri = f'/{fields[1]}/{unsurt(fields[0])}'

        return redirect(redirect_uri, code=302)

    urir = ''
    if lines_with_same_urir:
        msg += f'<p>{len(lines_with_same_urir)} capture(s) available:</p><ul>'

        for line in lines_with_same_urir:
            fields = line.split(' ', 2)
            urir = unsurt(fields[0])
            msg += (f'<li><a href="/{fields[1]}/{urir}">{urir} at {fields[1]}'
                    f'</a></li>')
        msg += '</ul>'

    msg += '<p>TimeMaps: '
    msg += f'<a href="/timemap/link/{urir}">Link</a> '
    msg += f'<a href="/timemap/cdxj/{urir}">CDXJ</a> '

    resp = Response(msg, status=404)
    link_header = get_link_header_abbreviated_timemap(path, datetime)

    # By default, a TM has a self-reference URI-T
    link_header = link_header.replace('self timemap', 'timemap')

    resp.headers['Link'] = link_header

    return resp


def extract_response_from_chunked_data(data):
    retStr = ''

    if isinstance(data, bytes):
        data = data.decode()
    (chunk_descriptor, rest) = data.split('\n', 1)
    chunk_descriptor = chunk_descriptor.split(';')[0].strip()

    while chunk_descriptor != '0':
        # On fail, exception, delta in header vs. payload chunkedness
        chunk_dec_from_hex = int(chunk_descriptor, 16)  # Get dec for slice

        retStr += rest[:chunk_dec_from_hex]  # Add to payload
        rest = rest[chunk_dec_from_hex:]  # Trim from the next chunk onward

        (CRLF, chunk_descriptor, rest) = rest.split('\n', 2)
        chunk_descriptor = chunk_descriptor.split(';')[0].strip()

        if len(chunk_descriptor.strip()) == 0:
            break

    return retStr


def generate_daemon_status_button():
    text = 'Not Running'
    button_text = 'Start'

    try:
        ipwb_utils.check_daemon_is_alive()

    except IPFSDaemonNotAvailable:
        pass

    else:
        text = 'Running'
        button_text = 'Stop'

    status_page_html = f'<html id="status{button_text}" class="status">'
    status_page_html += ('<head><base href="/ipwbassets/" />'
                         '<link rel="stylesheet" type="text/css" '
                         'href="webui.css" />'
                         '<script src="webui.js"></script>'
                         '<script src="daemonController.js"></script>'
                         '</head><body>')
    button_html = f'<span id="status">{text}</span>'
    button_html += f'<button id="daeAction">{button_text}</button>'

    footer = '<script>assignStatusButtonHandlers()</script></body></html>'
    return Response(f'{status_page_html}{button_html}{footer}')


def get_index_file_full_path(cdxj_file_path=INDEX_FILE):
    # Avoid prepending current directory path to an IPFS hash.
    if cdxj_file_path.startswith('Qm'):
        return cdxj_file_path

    index_file_path = f'/{cdxj_file_path}'.replace('ipwb.replay', 'ipwb')

    if os.path.isfile(cdxj_file_path):
        return cdxj_file_path

    index_file_name = pkg_resources.resource_filename(
        __name__, index_file_path)
    return index_file_name


def get_uris_and_datetimes_in_cdxj(cdxj_file_path=INDEX_FILE):
    index_file_contents = get_web_archive_index(cdxj_file_path)

    if not index_file_contents:
        return 0

    lines = index_file_contents.strip().split('\n')

    uris = {}
    for i, l in enumerate(lines):
        if not ipwb_utils.is_valid_cdxj_line(l):
            continue

        if ipwb_utils.is_cdxj_metadata_record(l):
            continue

        cdxj_fields = l.split(' ', 2)
        uri = unsurt(cdxj_fields[0])
        datetime = cdxj_fields[1]

        try:
            json_fields = json.loads(cdxj_fields[2])
        except Exception as e:  # Skip lines w/o JSON block
            continue

        if uri not in uris:
            uris[uri] = []

        memento_as_json = {
            'datetime': datetime,
            'mime': json_fields['mime_type'] or '',
            'status': json_fields['status_code']
        }
        if 'title' in json_fields:
            memento_as_json['title'] = json_fields['title']

        uris[uri].append(memento_as_json)

    return json.dumps(uris)


def calculate_memento_info_in_index(cdxj_file_path=INDEX_FILE):
    print(f'Retrieving URI-Ms from {cdxj_file_path}')
    index_file_contents = get_web_archive_index(cdxj_file_path)

    err_return = (0, 0)

    if not index_file_contents:
        return err_return

    lines = index_file_contents.strip().split('\n')

    if not lines:
        return err_return

    memento_info = {
        'memento_count': 0,
        'html_count': 0,
        'surt_uris': {},
        'oldest_datetime': None,
        'newest_datetime': None
    }

    for i, l in enumerate(lines):
        valid_cdxj_line = ipwb_utils.is_valid_cdxj_line(l)
        metadata_record = ipwb_utils.is_cdxj_metadata_record(l)
        if valid_cdxj_line and not metadata_record:
            memento_info['memento_count'] += 1
            (surt_uri, datetime, jsonInLine) = l.split(' ', 2)
            if surt_uri not in memento_info['surt_uris']:
                memento_info['surt_uris'][surt_uri] = 1
            else:  # Unnecessary to keep count now, maybe useful later
                memento_info['surt_uris'][surt_uri] += 1

            j = json.loads(jsonInLine)

            # Count only non-redirect HTML pages for html_count display
            if j['mime_type'] and \
                    j['mime_type'].lower().startswith('text/html') and \
                    j['status_code'][0] != '3':
                memento_info['html_count'] += 1

            if memento_info['oldest_datetime'] is None:
                memento_info['oldest_datetime'] = datetime
                memento_info['newest_datetime'] = datetime
                continue

            if datetime < memento_info['oldest_datetime']:
                memento_info['oldest_datetime'] = datetime
            if datetime > memento_info['newest_datetime']:
                memento_info['newest_datetime'] = datetime

    return memento_info


def objectify_cdxj_data(lines, only_uri):
    cdxj_data = {'metadata': [], 'data': []}
    for line in lines:
        if len(line.strip()) == 0:
            break
        if line[0] != '!':
            (surt, datetime, the_rest) = line.split(' ', 2)
            search_string = f"{surt} {datetime}"
            if only_uri:
                search_string = surt
            cdxj_data['data'].append(search_string)
        else:
            cdxj_data['metadata'].append(line)
    return cdxj_data


def binary_search(haystack, needle, returnIndex=False, only_uri=False):
    lBound = 0
    uBound = None

    surt_uris_and_datetimes = []

    cdxj_obj = objectify_cdxj_data(haystack, only_uri)
    surt_uris_and_datetimes = cdxj_obj['data']

    meta_line_count = len(cdxj_obj['metadata'])

    uBound = len(surt_uris_and_datetimes)

    pos = bisect_left(surt_uris_and_datetimes, needle, lBound, uBound)

    if pos != uBound and surt_uris_and_datetimes[pos] == needle:
        if returnIndex:  # Index useful for adjacent line searching
            return pos + meta_line_count
        return haystack[pos + meta_line_count]
    else:
        return None


def get_cdxj_line_binarySearch(
         surt_uri, cdxj_file_path=INDEX_FILE, retIndex=False, only_uri=False):
    full_file_path = get_index_file_full_path(cdxj_file_path)

    content = get_web_archive_index(full_file_path)

    lines = content.split('\n')

    line_found = binary_search(lines, surt_uri, retIndex, only_uri)
    if line_found is None:
        print(f"Could not find {surt_uri} in CDXJ at {full_file_path}")

    return line_found


def start(cdxj_file_path, proxy=None):
    host_port = ipwb_utils.get_ipwb_replay_config()
    app.proxy = proxy

    if not host_port:
        ipwb_utils.set_ipwb_replay_config(IPWBREPLAY_HOST, IPWBREPLAY_PORT)

    # This will throw an exception if daemon is not available.
    ipwb_utils.check_daemon_is_alive()

    ipwb_utils.set_ipwb_replay_index_path(cdxj_file_path)
    app.cdxj_file_path = cdxj_file_path

    try:
        print((f'IPWB replay started on '
               f'http://{IPWBREPLAY_HOST}:{IPWBREPLAY_PORT}'))

        app.run(host='0.0.0.0', port=IPWBREPLAY_PORT)
    except gaierror:
        print('Detected no active Internet connection.')
        print('Overriding to use default IP and port configuration.')
        app.run()
    except socketerror:
        print(f'Address {IPWBREPLAY_HOST}:{IPWBREPLAY_PORT} already in use!')
        sys.exit()


# Read in URI, convert to SURT
#  surt(uriIn)
# Get SURTed URI lines in CDXJ
#  Read CDXJ
#  Do bin search to find relevant lines

# read IPFS hash from relevant lines (header, payload)

# Fetch IPFS data at hashes
