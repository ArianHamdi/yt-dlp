import functools
import urllib.parse

from .common import InfoExtractor
from ..networking.exceptions import HTTPError
from ..utils import (
    ExtractorError,
    OnDemandPagedList,
    int_or_none,
    mimetype2ext,
    parse_iso8601,
    update_url_query,
    url_or_none,
)
from ..utils.traversal import traverse_obj

# Optional language prefix of localized pages, e.g. /de/fotos/... or /pt-br/...
_LANG_RE = r'(?:[a-z]{2}(?:-[a-z]{2})?/)'


class UnsplashBaseIE(InfoExtractor):
    _API_BASE = 'https://api.unsplash.com'
    _PAGE_SIZE = 30  # API maximum
    # Downscaled widths to offer; the site's own download menu uses 640/1920/2400
    _WIDTHS = (400, 640, 1080, 1920, 2400, 3840, 5120)

    def _access_key(self):
        # One key for every Unsplash extractor, read from the shared "unsplash" namespace
        key = self._configuration_arg('access_key', [None], ie_key='unsplash', casesense=True)[0]
        if not key:
            raise ExtractorError(
                'Unsplash requires an API access key. Create a free application at '
                'https://unsplash.com/oauth/applications and pass its Access Key with '
                '--extractor-args "unsplash:access_key=YOUR_KEY"', expected=True)
        return key

    def _call_api(self, path, item_id, note='Downloading JSON metadata', query=None, fatal=True):
        url = path if path.startswith('http') else f'{self._API_BASE}/{path}'
        try:
            res = self._download_json_handle(
                url, item_id, note, query=query, fatal=fatal, headers={
                    'Accept-Version': 'v1',
                    'Authorization': f'Client-ID {self._access_key()}',
                })
        except ExtractorError as e:
            if not isinstance(e.cause, HTTPError):
                raise
            status = e.cause.status
            body = e.cause.response.read().decode(errors='replace')
            if status == 401:
                raise ExtractorError('Unsplash rejected the API access key', expected=True)
            if status == 403 and 'rate limit' in body.lower():
                raise ExtractorError(
                    'Unsplash API rate limit exceeded (50 requests/hour for demo applications); '
                    'try again later', expected=True)
            if status == 404:
                raise ExtractorError('This item does not exist on Unsplash', expected=True)
            raise
        return res or (None, None)  # non-fatal failures return False

    def _track_download(self, photo):
        # Unsplash's API guidelines require reporting each download. Skip it when nothing is
        # actually downloaded (-j, -F, --simulate, ...) so browsing doesn't inflate the stats
        if any(self.get_param(p) for p in ('simulate', 'skip_download', 'listformats')):
            return
        location = traverse_obj(photo, ('links', 'download_location', {url_or_none}))
        if location:
            self._call_api(
                location, photo.get('id'), 'Reporting download to Unsplash', fatal=False)

    def _image_formats(self, raw_url, video_id, width=None, height=None, need_meta=True):
        """Every size Unsplash's imgix CDN can render for this image.

        imgix re-encodes every request (even a bare URL comes back at its default quality
        without EXIF), so the untouched upload is never available; the best is a
        full-resolution, maximum-quality render.
        """
        # imgix describes the source image when asked for fm=json; it doesn't count against the API
        # quota. Only needed when the API hasn't given the size, or the source may not be a JPEG
        meta = self._download_json(
            update_url_query(raw_url, {'fm': 'json'}), video_id,
            'Downloading image metadata', fatal=False) if need_meta or not (width and height) else None
        meta = meta or {}
        width = width or int_or_none(meta.get('PixelWidth'))
        height = height or int_or_none(meta.get('PixelHeight'))
        # Keep PNG/WebP sources (e.g. transparent illustrations) in their format; JPEG otherwise
        ext = mimetype2ext(meta.get('Content-Type'))
        if ext not in ('png', 'webp'):
            ext = 'jpg'

        def fmt(format_id, quality, w=None, note=None, preference=None):
            return {
                'format_id': format_id,
                'format_note': note,
                'url': update_url_query(raw_url, {
                    # cs=srgb converts wide-gamut sources (e.g. Adobe RGB) so they look right
                    # everywhere, as Unsplash's own download button does
                    'fm': ext, 'cs': 'srgb', 'fit': 'max',
                    **({'q': quality} if ext == 'jpg' else {}), **({'w': w} if w else {})}),
                'ext': ext,
                'width': w or width,
                'height': (round(height * w / width) if width and height else None) if w else height,
                'quality': preference,
            }

        return [
            # fit=max never upscales, so only widths below the full size are worth listing
            *(fmt(str(w), 85, w=w) for w in self._WIDTHS if not width or w < width),
            # Lossless formats have no quality setting, so this would duplicate "best"
            *([fmt('full', 85, note='full size, as Unsplash\'s download button')] if ext == 'jpg' else []),
            fmt('best', 100, note='full size, maximum quality', preference=1),
        ]

    def _parse_photo(self, photo):
        photo_id = photo['id']
        raw_url = traverse_obj(photo, ('urls', 'raw', {url_or_none}))
        if not raw_url:
            raise ExtractorError('No image URL found', video_id=photo_id)

        thumbnails = [{
            'id': key,
            'url': photo['urls'][key],
            'preference': -idx,
        } for idx, key in enumerate(('regular', 'small', 'thumb'))
            if traverse_obj(photo, ('urls', key, {url_or_none}))]

        user = photo.get('user') or {}
        return {
            'id': photo_id,
            'title': traverse_obj(photo, (('description', 'alt_description'), {str.strip}, filter, any)) or photo_id,
            # Photos are JPEGs; illustrations can be PNGs (possibly transparent), so look those up
            'formats': self._image_formats(
                raw_url, photo_id, int_or_none(photo.get('width')), int_or_none(photo.get('height')),
                need_meta=photo.get('asset_type') not in (None, 'photo')),
            'thumbnails': thumbnails,
            'license': 'Unsplash+ License' if photo.get('premium') or photo.get('plus') else 'Unsplash License',
            'uploader': user.get('name'),
            'uploader_id': user.get('username'),
            'uploader_url': traverse_obj(user, ('links', 'html', {url_or_none})),
            'webpage_url': traverse_obj(photo, ('links', 'html', {url_or_none})),
            'tags': traverse_obj(photo, ('tags', ..., 'title', {str})) or None,
            'location': traverse_obj(photo, ('location', 'name', {str})),
            **traverse_obj(photo, {
                'description': ('description', {str}, {str.strip}, filter),
                'alt_title': ('alt_description', {str}, {str.strip}, filter),
                'timestamp': ('created_at', {parse_iso8601}),
                'modified_timestamp': ('updated_at', {parse_iso8601}),
                'like_count': ('likes', {int_or_none}),
                'view_count': ('views', {int_or_none}),
            }),
        }

    def _fetch_page(self, path, playlist_id, page):
        photos, _ = self._call_api(
            path, playlist_id, f'Downloading page {page + 1}',
            query={'page': page + 1, 'per_page': self._PAGE_SIZE})
        # Entries point at the photo pages instead of carrying formats: listing (--flat-playlist)
        # then costs no extra API calls, and only the photos actually picked get fetched and
        # reported as downloaded
        for photo in traverse_obj(photos, lambda _, v: v['id']):
            yield self.url_result(
                traverse_obj(photo, ('links', 'html', {url_or_none}))
                or f'https://unsplash.com/photos/{photo["id"]}',
                UnsplashIE, photo['id'], **traverse_obj(photo, {
                    'title': (('description', 'alt_description'), {str}, {str.strip}, filter, any),
                    'uploader': ('user', 'name', {str}),
                    'uploader_id': ('user', 'username', {str}),
                    'width': ('width', {int_or_none}),
                    'height': ('height', {int_or_none}),
                    'thumbnail': ('urls', 'small', {url_or_none}),
                }))

    def _photo_entries(self, path, playlist_id):
        return OnDemandPagedList(
            functools.partial(self._fetch_page, path, playlist_id), self._PAGE_SIZE)


class UnsplashIE(UnsplashBaseIE):
    IE_NAME = 'unsplash'
    IE_DESC = 'Unsplash photos and illustrations (needs a free API key: --extractor-args "unsplash:access_key=KEY")'
    _VALID_URL = rf'''(?x)
        https?://(?:www\.)?unsplash\.com/
        (?:photos|illustrations|{_LANG_RE}[^/?#]+)/
        (?:[^/?#]*-)?(?P<id>[\w-]{{11}})/?(?:[?#]|$)
    '''
    _TESTS = [{
        'url': 'https://unsplash.com/photos/close-up-photo-of-white-dandelion-iTNKDmqC2tM',
        'info_dict': {
            'id': 'iTNKDmqC2tM',
            'ext': 'jpg',
            'title': str,
            'alt_title': 'close up photo of white dandelion',
            'uploader': str,
            'uploader_id': str,
            'timestamp': int,
            'license': 'Unsplash License',
        },
        'skip': 'Requires an Unsplash API access key',
    }, {
        # ID contains a hyphen
        'url': 'https://unsplash.com/photos/silhouette-of-mountains-during-golden-hour-zXt-oJtxVYM',
        'only_matching': True,
    }, {
        'url': 'https://unsplash.com/photos/iTNKDmqC2tM',
        'only_matching': True,
    }, {
        'url': 'https://unsplash.com/de/fotos/nahaufnahme-von-weissem-lowenzahn-iTNKDmqC2tM',
        'only_matching': True,
    }, {
        'url': 'https://unsplash.com/ja/%E5%86%99%E7%9C%9F/%E7%99%BD%E3%81%84%E3%82%BF%E3%83%B3%E3%83%9D%E3%83%9D%E3%81%AE%E6%8E%A5%E5%86%99%E5%86%99%E7%9C%9F-iTNKDmqC2tM',
        'only_matching': True,
    }, {
        'url': 'https://unsplash.com/illustrations/a-drawing-of-a-mountain-range-AbCdEfGhIjK',
        'only_matching': True,
    }]

    def _real_extract(self, url):
        photo_id = self._match_id(url)
        photo, _ = self._call_api(f'photos/{photo_id}', photo_id)
        self._track_download(photo)
        return self._parse_photo(photo)


class UnsplashCollectionIE(UnsplashBaseIE):
    IE_NAME = 'unsplash:collection'
    _VALID_URL = rf'''(?x)
        https?://(?:www\.)?unsplash\.com/
        # Localized collection pages always carry a slug; requiring it keeps bare photo links out
        (?:collections/|{_LANG_RE}[^/?#]+/(?=[\w-]+/[^/?#]))
        (?P<id>\d+|[\w-]{{11}})(?=[/?#]|$)
    '''
    _TESTS = [{
        'url': 'https://unsplash.com/collections/9530667/iphone-wallpaper',
        'info_dict': {
            'id': '9530667',
            'title': str,
        },
        'playlist_mincount': 30,
        'skip': 'Requires an Unsplash API access key',
    }, {
        'url': 'https://unsplash.com/collections/XduMi-lW8os/4k-wallpaper-for-desktop',
        'only_matching': True,
    }, {
        'url': 'https://unsplash.com/es/colecciones/XduMi-lW8os/4k-wallpaper-for-desktop',
        'only_matching': True,
    }, {
        'url': 'https://unsplash.com/ja/%E3%82%B3%E3%83%AC%E3%82%AF%E3%82%B7%E3%83%A7%E3%83%B3/9530667/iphone-wallpaper',
        'only_matching': True,
    }]

    def _real_extract(self, url):
        collection_id = self._match_id(url)
        collection, _ = self._call_api(f'collections/{collection_id}', collection_id)
        return self.playlist_result(
            self._photo_entries(f'collections/{collection_id}/photos', collection_id),
            collection_id, traverse_obj(collection, ('title', {str}, {str.strip}, filter)),
            traverse_obj(collection, ('description', {str}, {str.strip}, filter)),
            playlist_count=int_or_none(collection.get('total_photos')),
            uploader=traverse_obj(collection, ('user', 'name', {str})),
            uploader_id=traverse_obj(collection, ('user', 'username', {str})))


class UnsplashUserIE(UnsplashBaseIE):
    IE_NAME = 'unsplash:user'
    _VALID_URL = rf'https?://(?:www\.)?unsplash\.com/{_LANG_RE}?@(?P<id>[\w.-]+)(?:/(?P<tab>likes))?/?(?:[?#]|$)'
    _TESTS = [{
        'url': 'https://unsplash.com/@unsplash',
        'only_matching': True,
    }, {
        'url': 'https://unsplash.com/@unsplash/likes',
        'only_matching': True,
    }, {
        'url': 'https://unsplash.com/de/@unsplash',
        'only_matching': True,
    }]

    def _real_extract(self, url):
        username, tab = self._match_valid_url(url).group('id', 'tab')
        tab = tab or 'photos'
        username = urllib.parse.unquote(username)
        return self.playlist_result(
            self._photo_entries(f'users/{username}/{tab}', username),
            f'{username}-{tab}', f'{username} ({tab})', uploader_id=username)


class UnsplashImageIE(UnsplashBaseIE):
    IE_NAME = 'unsplash:image'
    IE_DESC = 'Direct images.unsplash.com links (no API key needed)'
    _VALID_URL = r'https?://(?:images|plus)\.unsplash\.com/(?P<id>(?:premium_)?photo-[\w-]+)'
    _TESTS = [{
        'url': 'https://images.unsplash.com/photo-1526827300700-4296004068cd?ixlib=rb-4.1.0&q=80&fm=jpg&w=1080',
        'info_dict': {
            'id': 'photo-1526827300700-4296004068cd',
            'ext': 'jpg',
            'title': 'photo-1526827300700-4296004068cd',
            'width': 3648,
            'height': 5472,
            'license': 'Unsplash License',
        },
    }]

    def _real_extract(self, url):
        image_id = self._match_id(url)
        # Drop the resizing parameters to get back to the original image
        raw_url = urllib.parse.urlunparse(urllib.parse.urlparse(url)._replace(query='', fragment=''))
        return {
            'id': image_id,
            'title': image_id,
            'formats': self._image_formats(raw_url, image_id),
            'license': 'Unsplash+ License' if image_id.startswith('premium_') else 'Unsplash License',
        }
