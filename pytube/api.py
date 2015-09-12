#!/usr/bin/env python
# -*- coding: utf-8 -*-
from __future__ import unicode_literals
from .exceptions import MultipleObjectsReturned, YouTubeError, CipherError
from .jsinterp import JSInterpreter
from .models import Video
from .utils import safe_filename
try:
    from urllib2 import urlopen
    from urlparse import urlparse, parse_qs, unquote
except ImportError:
    from urllib.parse import urlparse, parse_qs, unquote
    from urllib.request import urlopen

import re
import json
import warnings
import logging

log = logging.getLogger(__name__)

# YouTube quality and codecs id map.
# source: http://en.wikipedia.org/wiki/YouTube#Quality_and_codecs
YT_ENCODING = {
    # Flash Video
    5: ["flv", "240p", "Sorenson H.263", "N/A", "0.25", "MP3", "64"],
    6: ["flv", "270p", "Sorenson H.263", "N/A", "0.8", "MP3", "64"],
    34: ["flv", "360p", "H.264", "Main", "0.5", "AAC", "128"],
    35: ["flv", "480p", "H.264", "Main", "0.8-1", "AAC", "128"],

    # 3GP
    36: ["3gp", "240p", "MPEG-4 Visual", "Simple", "0.17", "AAC", "38"],
    13: ["3gp", "N/A", "MPEG-4 Visual", "N/A", "0.5", "AAC", "N/A"],
    17: ["3gp", "144p", "MPEG-4 Visual", "Simple", "0.05", "AAC", "24"],

    # MPEG-4
    18: ["mp4", "360p", "H.264", "Baseline", "0.5", "AAC", "96"],
    22: ["mp4", "720p", "H.264", "High", "2-2.9", "AAC", "192"],
    37: ["mp4", "1080p", "H.264", "High", "3-4.3", "AAC", "192"],
    38: ["mp4", "3072p", "H.264", "High", "3.5-5", "AAC", "192"],
    82: ["mp4", "360p", "H.264", "3D", "0.5", "AAC", "96"],
    83: ["mp4", "240p", "H.264", "3D", "0.5", "AAC", "96"],
    84: ["mp4", "720p", "H.264", "3D", "2-2.9", "AAC", "152"],
    85: ["mp4", "1080p", "H.264", "3D", "2-2.9", "AAC", "152"],

    # WebM
    43: ["webm", "360p", "VP8", "N/A", "0.5", "Vorbis", "128"],
    44: ["webm", "480p", "VP8", "N/A", "1", "Vorbis", "128"],
    45: ["webm", "720p", "VP8", "N/A", "2", "Vorbis", "192"],
    46: ["webm", "1080p", "VP8", "N/A", "N/A", "Vorbis", "192"],
    100: ["webm", "360p", "VP8", "3D", "N/A", "Vorbis", "128"],
    101: ["webm", "360p", "VP8", "3D", "N/A", "Vorbis", "192"],
    102: ["webm", "720p", "VP8", "3D", "N/A", "Vorbis", "192"]
}

# The keys corresponding to the quality/codec map above.
YT_ENCODING_KEYS = (
    'extension',
    'resolution',
    'video_codec',
    'profile',
    'video_bitrate',
    'audio_codec',
    'audio_bitrate'
)


class YouTube(object):
    def __init__(self):
        self._filename = None
        self._fmt_values = []
        self._video_url = None
        self._js_code = False

    @property
    def url(self):
        """Exposes the video url."""
        return self._video_url

    @url.setter
    def url(self, url):
        """Defines the URL of the YouTube video."""
        warnings.warn("url setter deprecated, use `from_url()` "
                      "instead.", DeprecationWarning)
        self.from_url(url)

    def from_url(self, url):
        self._video_url = url
        # Reset the filename.
        self._filename = None
        # Get the video details.
        self._get_video_info()

    @property
    def filename(self):
        """Exposes the title of the video. If this is not set, one is generated
        based on the name of the video.
        """
        if not self._filename:
            self._filename = safe_filename(self.title)
        return self._filename

    @filename.setter
    def filename(self, filename):
        """Defines the filename."""
        warnings.warn("filename setter deprecated, use `set_filename()` "
                      "instead.", DeprecationWarning)
        self.set_filename(filename)

    def set_filename(self, filename):
        """Defines the filename."""
        self._filename = filename
        if self.videos:
            for video in self.videos:
                video.filename = filename

    @property
    def video_id(self):
        """Gets the video ID extracted from the URL."""
        parts = urlparse(self._video_url)
        qs = getattr(parts, 'query')
        if qs:
            video_id = parse_qs(qs).get('v')
            if video_id:
                return video_id.pop()

    def get(self, extension=None, resolution=None, profile=None):
        """Return a single video given an extention and resolution.

        :param str extention:
            The desired file extention (e.g.: mp4).
        :param str resolution:
            The desired video broadcasting standard.
        :param str profile:
            The desired quality profile.
        """
        result = []
        for v in self.videos:
            if extension and v.extension != extension:
                continue
            elif resolution and v.resolution != resolution:
                continue
            elif profile and v.profile != profile:
                continue
            else:
                result.append(v)
        if not len(result):
            return
        elif len(result) is 1:
            return result[0]
        else:
            raise MultipleObjectsReturned(
                "get() returned more than one object")

    def filter(self, extension=None, resolution=None, profile=None):
        """Return a filtered list of videos given an extention and resolution
        criteria.

        :param str extention:
            The desired file extention (e.g.: mp4).
        :param str resolution:
            The desired video broadcasting standard.
        :param str profile:
            The desired quality profile.
        """
        results = []
        for v in self.videos:
            if extension and v.extension != extension:
                continue
            elif resolution and v.resolution != resolution:
                continue
            elif profile and v.profile != profile:
                continue
            else:
                results.append(v)
        return results

    def _parse_stream_map(self, text):
        """Python's `parse_qs` can't properly decode the stream map
        containing video data so we use this instead.
        """
        videoinfo = {
            "itag": [],
            "url": [],
            "quality": [],
            "fallback_host": [],
            "s": [],
            "type": []
        }

        # Split individual videos
        videos = text.split(",")
        # Unquote the characters and split to parameters
        videos = [video.split("&") for video in videos]

        for video in videos:
            for kv in video:
                key, value = kv.split("=")
                videoinfo.get(key, []).append(unquote(value))

        return videoinfo

    def _get_video_info(self):
        """This is responsable for executing the request, extracting the
        necessary details, and populating the different video resolutions and
        formats into a list.
        """
        self.title = None
        self.videos = []

        response = urlopen(self.url)

        if not response:
            return False
        body = response.read().decode("utf-8")

        if "og:restrictions:age" in body:
            raise YouTubeError("Unable to fetch age restricted content.")

        json_data = self._get_json_data(body)

        if not json_data:
            raise YouTubeError("Unable to extract json.")

        encoded_stream_map = json_data.get("args", {}).get(
            "url_encoded_fmt_stream_map")
        stream_map = self._parse_stream_map(encoded_stream_map)

        self.title = json_data.get("args", {}).get("title")
        js_url = "http:{}".format(json_data["assets"]["js"])
        video_urls = stream_map["url"]

        for i, url in enumerate(video_urls):
            try:
                fmt, fmt_data = self._extract_fmt(url)
            except (TypeError, KeyError):
                continue

            # If the signature must be ciphered...
            if "signature=" not in url:
                signature = self._get_cipher(stream_map["s"][i], js_url)
                url = "{}&signature={}".format(url, signature)

            self.videos.append(Video(url, self.filename, **fmt_data))
            self._fmt_values.append(fmt)
        self.videos.sort()

    def _get_json_data(self, body):
        """Isolates and parses the json stream from the html content.

        :param str body: The content body of the YouTube page.
        """
        # Note: the number 18 represents the length of "ytplayer.config = ".
        start = body.find("ytplayer.config = ") + 18
        body = body[start:]
        offset = self._find_json_offset(body)

        if not offset:
            return None
        return json.loads(body[:offset])

    def _find_json_offset(self, body):
        """Finds the variable offset of where the json starts using bracket
        matching/counting.

        :param str body: The content body of the YouTube page.
        """
        bracket_count = 0
        index = 1
        for i, char in enumerate(body):
            if char == "{":
                bracket_count += 1
            elif char == "}":
                bracket_count -= 1
                if bracket_count == 0:
                    break
        else:
            return None
        return index + i

    def _get_cipher(self, signature, url):
        """Get the signature using the cipher
        implemented in the JavaScript code

        :param str signature:
            Signature.
        :param str url:
            url of JavaScript file.
        """
        # Getting JS code (if hasn't downloaded yet)
        if not self._js_code:
            # TODO: don't use conditional expression if line > 79 characters.
            self._js_code = (urlopen(url).read().decode()
                             if not self._js_code else self._js_code)

        try:
            mobj = re.search(r'\.sig\|\|([a-zA-Z0-9$]+)\(', self._js_code)
            if mobj:
                # return the first matching group
                funcname = next(g for g in mobj.groups() if g is not None)

            jsi = JSInterpreter(self._js_code)
            initial_function = jsi.extract_function(funcname)
            return initial_function([signature])
        except Exception as e:
            raise CipherError("Couldn't cipher the signature. Maybe YouTube "
                "has changed the cipher algorithm. Notify this issue on "
                "GitHub: %s" % e)

    def _extract_fmt(self, text):
        """YouTube does not pass you a completely valid URLencoded form, I
        suspect this is suppose to act as a deterrent.. Nothing some regex
        couldn't handle.

        :param str text:
            The malformed data contained within each url node.
        """
        itag = re.findall('itag=(\d+)', text)
        if itag and len(itag) is 1:
            itag = int(itag[0])
            attr = YT_ENCODING.get(itag, None)
            if not attr:
                return itag, None
            return itag, dict(zip(YT_ENCODING_KEYS, attr))
