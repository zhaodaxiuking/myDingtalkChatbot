import base64
import hashlib
import hmac
import json
import time
import urllib.parse
from pathlib import Path

import requests
from requests import RequestException


class DingtalkSender:
    def __init__(self, webhook, secret=None, imgbed_upload_url=None):
        self.webhook = webhook
        self.secret = secret
        self.imgbed_upload_url = (imgbed_upload_url or '').strip()

    def _validate_imgbed_upload_url(self):
        if not self.imgbed_upload_url:
            raise ValueError('未配置图床上传地址，请先在首页保存图床配置')
        normalized = self.imgbed_upload_url.lower()
        if '/upload' not in normalized:
            raise ValueError(
                f'当前图床上传地址不是上传接口：{self.imgbed_upload_url}。请在首页将图床地址改为 /upload 接口地址后再重试。'
            )

    def _signed_webhook(self):
        if not self.secret:
            return self.webhook
        timestamp = str(round(time.time() * 1000))
        secret_enc = self.secret.encode('utf-8')
        string_to_sign = '{}\n{}'.format(timestamp, self.secret)
        string_to_sign_enc = string_to_sign.encode('utf-8')
        hmac_code = hmac.new(secret_enc, string_to_sign_enc, digestmod=hashlib.sha256).digest()
        sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
        sep = '&' if '?' in self.webhook else '?'
        return f'{self.webhook}{sep}timestamp={timestamp}&sign={sign}'

    def upload_local_image(self, image_path):
        self._validate_imgbed_upload_url()
        image_path = Path(image_path)
        if not image_path.exists():
            raise FileNotFoundError(str(image_path))

        last_error = None
        for attempt in range(1, 4):
            try:
                with image_path.open('rb') as f:
                    files = {'file': (image_path.name, f, 'image/png')}
                    resp = requests.post(self.imgbed_upload_url, files=files, timeout=90)
                resp.raise_for_status()
                data = resp.json()
                if not isinstance(data, list) or not data:
                    raise ValueError(f'unexpected upload response: {data}')
                src = data[0].get('src')
                if not src:
                    raise ValueError(f'upload response missing src: {data}')
                if src.startswith('http://') or src.startswith('https://'):
                    return src
                base = self.imgbed_upload_url.split('/upload', 1)[0]
                if src.startswith('/'):
                    return base + src
                return base + '/' + src
            except RequestException as exc:
                last_error = exc
                if attempt >= 3:
                    break
                time.sleep(1.2 * attempt)

        raise ConnectionError(f'图床上传失败，已重试 3 次: {last_error}')

    def send_image(self, image_url):
        payload = {
            'msgtype': 'markdown',
            'markdown': {
                'title': '图片通知',
                'text': f'![image]({image_url})'
            }
        }
        return self._post(payload)

    def send_markdown(self, title, text):
        payload = {
            'msgtype': 'markdown',
            'markdown': {
                'title': title,
                'text': text
            }
        }
        return self._post(payload)

    def send_local_markdown(self, title, text, image_path, append_image_url=True):
        image_url = self.upload_local_image(image_path)
        final_text = text
        if append_image_url:
            final_text = f'{text}\n\n![image]({image_url})'
        result = self.send_markdown(title=title, text=final_text)
        return {'upload_url': image_url, 'send_result': result}

    def send_markdown_with_images(self, title, text, image_urls=None):
        image_urls = image_urls or []
        final_text = text or ''
        if image_urls:
            image_block = '\n\n'.join(f'![image]({url})' for url in image_urls if url)
            if image_block:
                final_text = f'{final_text}\n\n{image_block}' if final_text else image_block
        result = self.send_markdown(title=title, text=final_text)
        return {'image_urls': image_urls, 'send_result': result}

    def _post(self, payload):
        resp = requests.post(
            self._signed_webhook(),
            headers={'Content-Type': 'application/json; charset=utf-8'},
            data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
            timeout=60,
        )
        resp.raise_for_status()
        return resp.json()
