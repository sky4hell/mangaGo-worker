"""
mangaGo Worker AOT 修图模块
从 comic-backend/bulePrint/comicCrawlRetouchBp.py 提取，供 worker 本地调用
"""
import os
import sys
import json
import logging

_logger = logging.getLogger(__name__)

# 检测 torch 是否可用
try:
    import torch
    _HAS_TORCH = True
except ImportError:
    _HAS_TORCH = False

# 与本地 AOT 一致的修图配置
RETOUCH_CONFIG = {
    "render": {
        "font_scale_factor": 3.0,
        "line_spacing_ratio": 1.3,
        "text_padding_ratio": 1.0,
        "font_size_offset": 15,
        "font_size": 100
    },
    "text_merge": {"enabled": True},
    "mask_expand_ratio": 0.05
}


def build_ocr_metadata(images_batch, use_translation=False):
    """构建 OCR metadata dict，block_id 精确匹配 + 下标回退"""
    all_metadata = {}
    for i, img in enumerate(images_batch):
        raw_ocr = img.get('ocrMetadata') or '[]'
        corrected = img.get('correctedTranslatedText') or ''
        translated = img.get('translatedText') or ''
        if use_translation:
            raw_trans = translated or corrected or '[]'
        else:
            raw_trans = corrected or translated or '[]'
        img_id = img.get('imageId') or img.get('id', '?')

        if raw_ocr == '[]':
            _logger.warning(f"[Worker修图] 图片{img_id} ocrMetadata为空，跳过")
            continue

        try:
            blocks = json.loads(raw_ocr)
            try:
                trans_list = json.loads(raw_trans)
                if isinstance(trans_list, str):
                    trans_list = [{'text': trans_list}]
                elif not isinstance(trans_list, list):
                    trans_list = []
            except (json.JSONDecodeError, TypeError):
                trans_list = [{'text': raw_trans}] if raw_trans and raw_trans != '[]' else []
        except (json.JSONDecodeError, TypeError) as e:
            _logger.error(f"[Worker修图] 图片{img_id} JSON解析失败: {e}")
            raise

        # block_id 匹配，匹配不到用下标
        trans_by_id = {}
        for t in trans_list:
            if isinstance(t, dict) and 'block_id' in t:
                trans_by_id[t['block_id']] = t.get('text', '')
        for j, block in enumerate(blocks):
            bid = block.get('block_id', None)
            if bid is not None and bid in trans_by_id:
                block['text'] = trans_by_id[bid]
            elif j < len(trans_list):
                txt = trans_list[j]
                block['text'] = txt if isinstance(txt, str) else (txt.get('text', '') if isinstance(txt, dict) else '')
            else:
                block['text'] = ''

        all_metadata[f"image_{i}"] = {"blocks": blocks}
    return all_metadata


def retouch_aot(image_paths, images_batch, retouch_config=None):
    """AOT-GAN 擦除 + Pillow 渲染译文"""
    from utils.local_lama import create_mask_from_blocks, simple_inpaint_aot
    from PIL import Image, ImageDraw, ImageFont
    import cv2
    import numpy as np

    if retouch_config is None:
        retouch_config = RETOUCH_CONFIG

    # 构建 OCR metadata
    ocr_metadata = build_ocr_metadata(images_batch)

    results_data = []
    for idx, path in enumerate(image_paths):
        meta_key = f"image_{idx}"
        meta = ocr_metadata.get(meta_key, {})
        blocks = meta.get('blocks', [])
        img_basename = os.path.basename(path)

        # 读图
        try:
            pil_img = Image.open(path).convert('RGB')
            img_cv_bgr = cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR)
        except Exception as e:
            _logger.error(f"[Worker修图] 图片读取失败 {img_basename}: {e}")
            results_data.append({'status': 'failed', 'error': f'图片读取失败: {e}'})
            continue

        h, w = img_cv_bgr.shape[:2]

        # mask + AOT 擦除
        ratio = retouch_config.get('mask_expand_ratio', 0.05)
        mask = create_mask_from_blocks(h, w, blocks, expand_ratio=ratio)
        has_mask = np.sum(mask > 0) > 0
        if not has_mask:
            results_data.append({'status': 'success', 'output_image': None})
            continue

        aot_ok, aot_result = simple_inpaint_aot(path, mask)
        if aot_ok:
            cvimg = aot_result.copy()
        else:
            cvimg = cv2.inpaint(img_cv_bgr, mask, 3, cv2.INPAINT_TELEA)
            _logger.info(f"[Worker修图] {img_basename}: AOT失败，回退OpenCV")

        # 字体
        fp = None
        for fc in ['C:/Windows/Fonts/arial.ttf', 'C:/Windows/Fonts/tahoma.ttf',
                    'C:/Windows/Fonts/segoeui.ttf', 'C:/Windows/Fonts/msyh.ttc',
                    'C:/Windows/Fonts/meiryo.ttc', 'C:/Windows/Fonts/msgothic.ttc']:
            if os.path.exists(fc):
                fp = fc
                break

        def _mkf(sz):
            try:
                return ImageFont.truetype(fp, sz) if fp else ImageFont.load_default()
            except Exception:
                return ImageFont.load_default()

        # 碰撞合并
        merged = []
        used = set()
        for i, a in enumerate(blocks):
            if i in used:
                continue
            ax1 = int(a.get('minX', 0)); ay1 = int(a.get('minY', 0))
            ax2 = int(a.get('maxX', 0)); ay2 = int(a.get('maxY', 0))
            atxt = (a.get('text') or '').strip()
            if not atxt:
                used.add(i)
                continue
            afs = a.get('font_size')
            if not isinstance(afs, (int, float)) or afs <= 10:
                afs = max(12, int((ay2 - ay1) * 0.85))
            for j, b in enumerate(blocks):
                if j <= i or j in used:
                    continue
                btxt = (b.get('text') or '').strip()
                if not btxt:
                    continue
                bx1 = int(b.get('minX', 0)); by1 = int(b.get('minY', 0))
                bx2 = int(b.get('maxX', 0)); by2 = int(b.get('maxY', 0))
                if ax2 < bx1 or bx2 < ax1:
                    continue
                dist = by1 - ay2 if by1 > ay2 else ay1 - by2
                avg_h = ((ay2 - ay1) + (by2 - by1)) // 2
                if dist > avg_h * 0.5:
                    continue
                atxt += ' ' + btxt
                ax1 = min(ax1, bx1); ay1 = min(ay1, by1)
                ax2 = max(ax2, bx2); ay2 = max(ay2, by2)
                afs = max(afs, b.get('font_size', 14))
                used.add(j)
            used.add(i)
            merged.append({
                'text': atxt, 'x1': ax1, 'y1': ay1, 'x2': ax2, 'y2': ay2,
                'font_size': afs, 'text_color': a.get('text_color', {})
            })

        # 渲染
        pil_i = Image.fromarray(cv2.cvtColor(cvimg, cv2.COLOR_BGR2RGB))
        draw = ImageDraw.Draw(pil_i)
        for blk in merged:
            txt = blk['text']
            x1, y1, x2, y2 = blk['x1'], blk['y1'], blk['x2'], blk['y2']
            bw_orig, bh_orig = x2 - x1, y2 - y1
            exp_x = int(bw_orig * 0.1)
            exp_y = int(bh_orig * 0.1)
            x1 = max(0, x1 - exp_x)
            y1 = max(0, y1 - exp_y)
            x2 = x2 + exp_x
            y2 = y2 + exp_y
            bw = x2 - x1
            bh = y2 - y1
            vertical = bh > bw * 1.2

            fs = blk['font_size']
            if not isinstance(fs, (int, float)) or fs <= 10:
                fs = max(12, int(bh * 0.85))
            fs = max(fs, 14)

            # 文字色 / 描边色
            tc = blk.get('text_color', {})
            fg = tuple(tc.get('fg', [0, 0, 0]))
            bg = tuple(tc.get('bg', [255, 255, 255]))
            stroke_luma = bg[0] * 0.299 + bg[1] * 0.587 + bg[2] * 0.114
            stroke_color = (0, 0, 0) if stroke_luma > 128 else (255, 255, 255)

            if vertical:
                _draw_vertical_text(draw, txt, x1, y1, x2, y2, fs, fg, stroke_color, _mkf)
            else:
                _draw_text_fit(draw, txt, x1, y1, x2, y2, fs, fg, stroke_color, _mkf)

        # 编码输出
        import io
        buf = io.BytesIO()
        pil_i.save(buf, format='PNG')
        import base64 as b64
        results_data.append({
            'status': 'success',
            'output_image': b64.b64encode(buf.getvalue()).decode('utf-8')
        })

    return results_data


def _draw_text_fit(draw, text, x1, y1, x2, y2, base_fs, fill, stroke, mkf):
    """水平文字自适应缩放到框内"""
    from PIL import ImageDraw, ImageFont
    bw = x2 - x1
    for fs in range(int(base_fs), 8, -1):
        font = mkf(fs)
        bbox = draw.textbbox((0, 0), text, font=font)
        tw = bbox[2] - bbox[0]
        if tw <= bw * 0.95:
            break
    th = draw.textbbox((0, 0), text, font=font)[3] - draw.textbbox((0, 0), text, font=font)[1]
    cy = (y1 + y2) // 2
    ty = cy - th // 2
    draw.text((x1, ty), text, font=font, fill=fill, stroke_width=1, stroke_fill=stroke)


def _draw_vertical_text(draw, text, x1, y1, x2, y2, base_fs, fill, stroke, mkf):
    """竖排文字"""
    from PIL import ImageDraw, ImageFont
    cx = (x1 + x2) // 2
    chars = list(text)
    for fs in range(int(base_fs), 8, -1):
        font = mkf(fs)
        max_cw = max((draw.textbbox((0, 0), c, font=font)[2] for c in chars), default=fs)
        total_h = sum(draw.textbbox((0, 0), c, font=font)[3] for c in chars)
        if max_cw <= (x2 - x1) * 0.9 and total_h <= (y2 - y1) * 0.95:
            break
    cur_y = y1
    for c in chars:
        cw = draw.textbbox((0, 0), c, font=font)[2]
        ch = draw.textbbox((0, 0), c, font=font)[3]
        draw.text((cx - cw // 2, cur_y), c, font=font, fill=fill, stroke_width=1, stroke_fill=stroke)
        cur_y += ch


def process_images_aot(images_data, download_dir, retouch_config=None):
    """批量 AOT 修图，返回 {imageId: base64}"""
    results = {}
    image_paths = []
    batch = []

    for item in images_data:
        local_path = item.get('localPath', '')
        if local_path:
            fp = os.path.join(download_dir, local_path.replace('\\', '/'))
            if os.path.exists(fp):
                image_paths.append(fp)
                batch.append(item)
            else:
                _logger.warning(f"[Worker修图] 文件不存在: {fp}")
                results[item['imageId']] = ''

    if not image_paths:
        return results

    aot_results = retouch_aot(image_paths, batch, retouch_config)
    for i, r in enumerate(aot_results):
        if i < len(batch):
            img_id = batch[i]['imageId']
            if r.get('status') == 'success':
                results[img_id] = r.get('output_image', '')
            else:
                results[img_id] = ''  # 失败返回空

    return results
