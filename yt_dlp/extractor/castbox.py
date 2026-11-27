import datetime as dt
import functools
import hashlib

from .common import InfoExtractor
from ..utils import (
    ExtractorError,
    OnDemandPagedList,
    clean_html,
    determine_ext,
    float_or_none,
    int_or_none,
    mimetype2ext,
    parse_iso8601,
    str_or_none,
    url_or_none,
)
from ..utils.traversal import traverse_obj


class CastboxBaseIE(InfoExtractor):
    _API_BASE = 'https://everest.castbox.fm/data'
    # Character permutation applied to the MD5 hex digest by the web client
    _SIGN_PERMUTATION = (
        24, 13, 4, 19, 6, 0, 8, 21, 25, 7, 28, 1, 15, 31, 10, 9,
        17, 18, 22, 11, 27, 23, 2, 26, 12, 5, 29, 14, 20, 30, 16, 3)

    def _sign_query(self, query):
        # Mirrors the web client: sorted, unencoded query (plus r=1), salted with
        # 'evst' and the current UTC date, MD5'd and then shuffled
        date = dt.datetime.now(dt.UTC).strftime('%Y%m%d')
        params = {'r': '1', **query}
        payload = '&'.join(f'{k}={params[k]}' for k in sorted(params))
        digest = hashlib.md5(f'{payload}evst{date}'.encode()).hexdigest()
        return {
            'm': date,
            'n': ''.join(digest[i] for i in self._SIGN_PERMUTATION),
            'r': '1',
        }

    def _call_api(self, path, item_id, query, note='Downloading JSON metadata'):
        query = {k: str(v) for k, v in {**query, 'web': 1}.items()}
        response = self._download_json(
            f'{self._API_BASE}/{path}', item_id, note,
            query={**query, **self._sign_query(query)})
        if response.get('code') != 0:
            raise ExtractorError(
                f'Castbox API said: {response.get("msg") or "unknown error"}', expected=True)
        return response.get('data') or {}

    def _parse_episode(self, data):
        media_urls = traverse_obj(data, (('url', ('urls', ...)), {url_or_none}))
        is_video = bool(int_or_none(data.get('video')))

        formats = [{
            'url': media_url,
            'ext': determine_ext(media_url, default_ext=None) or ('mp4' if is_video else 'mp3'),
            'filesize': int_or_none(data.get('size')) if not idx else None,
            'vcodec': None if is_video else 'none',
            # Prefer the primary URL; the 'urls' list may contain alternate mirrors
            'quality': -idx,
        } for idx, media_url in enumerate(dict.fromkeys(media_urls))]

        subtitles = {}
        for transcript in traverse_obj(data, ('podcast_transcript', lambda _, v: url_or_none(v['url']))):
            ext = mimetype2ext(transcript.get('type'))
            if ext not in ('srt', 'vtt'):
                continue
            subtitles.setdefault(transcript.get('language') or 'und', []).append({
                'url': transcript['url'],
                'ext': ext,
            })

        return {
            'formats': formats,
            'subtitles': subtitles,
            **traverse_obj(data, {
                'id': ('eid', {str_or_none}),
                'title': ('title', {str}),
                'description': ('description', {clean_html}),
                'thumbnail': (('big_cover_url', 'cover_url', 'small_cover_url'), {url_or_none}, any),
                'duration': ('duration', {float_or_none(scale=1000)}),
                'timestamp': ('release_date', {parse_iso8601}),
                'view_count': ('play_count', {int_or_none}),
                'like_count': ('like_count', {int_or_none}),
                'comment_count': ('comment_count', {int_or_none}),
                'uploader': ('author', {str}, filter),
                'series': ('channel', 'title', {str}),
                'series_id': ('cid', {str_or_none}),
                'channel': ('channel', 'title', {str}),
                'channel_id': ('cid', {str_or_none}),
            }),
        }


class CastboxIE(CastboxBaseIE):
    IE_NAME = 'castbox'
    _VALID_URL = [
        r'https?://(?:www\.)?castbox\.fm/episode/(?:[^/?#]*-)?id(?P<cid>\d+)-id(?P<id>\d+)',
        r'https?://(?:www\.)?castbox\.fm/app/castbox/player/id(?P<cid>\d+)/id(?P<id>\d+)',
        r'https?://(?:www\.)?castbox\.fm/(?:ep|vb)/(?P<id>\d+)',
    ]
    _TESTS = [{
        'url': 'https://castbox.fm/episode/The-Layover-%E2%80%94-Abolish-Jet-Lag-id6161270-id779008359',
        'info_dict': {
            'id': '779008359',
            'ext': 'mp3',
            'title': 'The Layover — Abolish Jet Lag',
            'description': 'Adam, Sam, and Ben abolish Jet Lag.',
            'thumbnail': r're:https?://.+\.jpg',
            'duration': 4255.0,
            'timestamp': 1739381616,
            'upload_date': '20250212',
            'series': 'The Layover',
            'series_id': '6161270',
            'channel': 'The Layover',
            'channel_id': '6161270',
            'view_count': int,
            'like_count': int,
            'comment_count': int,
        },
        'params': {'skip_download': True},
    }, {
        'url': 'https://castbox.fm/episode/Episode-737---Live-Wire-(1992)%2C-Exploding-Water%2C-and-Pierce-Brosnan-id2030417-id1027642161',
        'info_dict': {
            'id': '1027642161',
            'ext': 'mp3',
            'title': 'Episode 737 - Live Wire (1992), Exploding Water, and Pierce Brosnan',
            'description': 'md5:9431740020f6248c6b38d2d67acfd878',
            'thumbnail': r're:https?://.+\.jpg',
            'duration': 3206.0,
            'timestamp': 1795518758,
            'upload_date': '20261124',
            'uploader': 'Movies, Films and Flix',
            'series': 'Movies, Films and Flix',
            'series_id': '2030417',
            'channel': 'Movies, Films and Flix',
            'channel_id': '2030417',
            'view_count': int,
            'like_count': int,
            'comment_count': int,
            'subtitles': {'en': 'count:2'},
        },
        'params': {'skip_download': True},
    }, {
        'url': 'https://castbox.fm/app/castbox/player/id2030417/id1027642161',
        'only_matching': True,
    }, {
        'url': 'https://castbox.fm/episode/id2030417-id1027642161',
        'only_matching': True,
    }, {
        'url': 'https://castbox.fm/ep/1027642161',
        'only_matching': True,
    }, {
        'url': 'https://castbox.fm/vb/1028614314',
        'only_matching': True,
    }]

    def _real_extract(self, url):
        episode_id = self._match_id(url)
        data = self._call_api(
            'episode/v4', episode_id, {'eid': episode_id, 'raw': 1}, 'Downloading episode JSON')
        if not data:
            raise ExtractorError('Episode not found', expected=True)
        return self._parse_episode(data)


class CastboxChannelIE(CastboxBaseIE):
    IE_NAME = 'castbox:channel'
    _VALID_URL = [
        r'https?://(?:www\.)?castbox\.fm/channel/(?:[^/?#]*-)?id(?P<id>\d+)/?(?:[?#]|$)',
        r'https?://(?:www\.)?castbox\.fm/ch/(?P<id>\d+)',
    ]
    _TESTS = [{
        'url': 'https://castbox.fm/channel/Movies,-Films-and-Flix-id2030417',
        'info_dict': {
            'id': '2030417',
            'title': 'Movies, Films and Flix',
            'description': 'md5:f86cfd3427c330d0c018b0097f01c06d',
            'uploader': 'Movies, Films and Flix',
            'thumbnail': r're:https?://.+',
        },
        'playlist_mincount': 900,
    }, {
        'url': 'https://castbox.fm/channel/id2030417',
        'only_matching': True,
    }, {
        'url': 'https://castbox.fm/ch/2030417',
        'only_matching': True,
    }]
    _PAGE_SIZE = 100

    def _fetch_page(self, channel_id, channel_title, page_num):
        page = self._call_api(
            'episode_list/v2', channel_id, {
                'cid': channel_id,
                'skip': page_num * self._PAGE_SIZE,
                'limit': self._PAGE_SIZE,
                'ascending': 0,
            }, f'Downloading episode list page {page_num + 1}')
        for episode in traverse_obj(page, ('episode_list', lambda _, v: v['eid'])):
            yield {
                'series': channel_title,
                'channel': channel_title,
                **self._parse_episode(episode),
                'webpage_url': f'https://castbox.fm/episode/id{channel_id}-id{episode["eid"]}',
                'extractor_key': CastboxIE.ie_key(),
                'extractor': CastboxIE.IE_NAME,
            }

    def _real_extract(self, url):
        channel_id = self._match_id(url)
        channel = self._call_api(
            'channel/v3', channel_id, {'cid': channel_id, 'raw': 1}, 'Downloading channel JSON')
        if not channel:
            raise ExtractorError('Channel not found', expected=True)

        title = traverse_obj(channel, ('title', {str}))
        entries = OnDemandPagedList(
            functools.partial(self._fetch_page, channel_id, title), self._PAGE_SIZE)
        return self.playlist_result(entries, channel_id, title, **traverse_obj(channel, {
            'description': ('description', {clean_html}),
            'uploader': ('author', {str}, filter),
            'thumbnail': (('big_cover_url', 'https_cover_url', 'small_cover_url'), {url_or_none}, any),
        }))
