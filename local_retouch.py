"""
本地 AOT 修图管线（从 translation-service/bulePrint/comicCrawlRetouchBp.py 迁出）
仅供 mangaGo-worker 桌面程序使用，不依赖 Flask / DB。
"""
import base64
import json
import logging
import os
import sys
import traceback

import cv2
import numpy as np
import requests
from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger('local_retouch')

# comic-backend 路径，提供 utils.local_lama 依赖（该文件未被清理）
_BACKEND_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'comic-backend')
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

# 检测运行环境：有 torch → 本地 GPU 修图；无 torch → HTTP 发翻译服务
try:
    import torch  # noqa: F401  仅用于检测 GPU 环境
    _HAS_TORCH = True
except ImportError:
    _HAS_TORCH = False

if _HAS_TORCH:
    from utils.local_lama import create_mask_from_blocks, simple_inpaint_aot  # noqa: F401

MANGA_TRANSLATOR_BASE = os.environ.get('MANGA_TRANSLATOR_BASE', 'http://localhost:8001')
RETOUCH_DIRECT_URL = f'{MANGA_TRANSLATOR_BASE}/review/render-direct/batch'

# 传给 manga-translator 的修图配置
RETOUCH_CONFIG = {
    "detector": {"detector": "default"},
    "ocr": {"ocr": "48px_ctc"},
    "translator": {"translator": "none"},
    "render": {
        "font_scale_factor": 3.0,
        "line_spacing_ratio": 1.3,
        "text_padding_ratio": 1.0,
        "font_size_offset": 15,
        "font_size": 100
    },
    "remove_watermark": True,
    "watermark_auto_detect": True,
    "text_merge": {"enabled": True},
    "mask_expand_ratio": 0.05
}


def build_retouch_config(font_size_offset=None):
    config = json.loads(json.dumps(RETOUCH_CONFIG))
    if font_size_offset is not None:
        config['render']['font_size_offset'] = int(font_size_offset)
    return config


def build_ocr_metadata(images_batch, use_translation=False):
    all_metadata = {}
    for i, img in enumerate(images_batch):
        raw_ocr = img.get('ocrMetadata') or '[]'
        # 优先用校正译文，没有则回退到机器翻译
        corrected = img.get('correctedTranslatedText') or ''
        translated = img.get('translatedText') or ''
        if use_translation:
            raw_trans = translated or corrected or '[]'
        else:
            raw_trans = corrected or translated or '[]'
        img_id = img.get('id')

        if raw_ocr == '[]':
            logger.warning(f"[本地修图] 图片{img_id} ocrMetadata为空，跳过")
            continue

        logger.info(f"[本地修图] 图片{img_id} ocrMetadata长度: {len(raw_ocr)}, trans长度: {len(raw_trans)}")
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
            logger.error(f"[本地修图] 图片{img_id} JSON解析失败: {e}")
            raise

        # 先用 block_id 匹配，匹配不到再用下标
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

        logger.info(f"[本地修图] 图片{img_id} 组装了 {len(blocks)} 个blocks")

        all_metadata[f"image_{i}"] = {"blocks": blocks}
    return all_metadata


def _retouch_via_http(image_paths, images_batch, ocr_metadata, retouch_config):
    """HTTP 远程修图 → render-direct（直接用 blocks 坐标，不重跑 OCR）"""
    results = []
    for idx, path in enumerate(image_paths):
        img_basename = os.path.basename(path)
        if f"image_{idx}" not in ocr_metadata or not ocr_metadata.get(f"image_{idx}", {}).get('blocks'):
            results.append({'status': 'success', 'output_image': None, 'image_id': img_basename})
            continue
        results.append(None)
    valid_indices = [i for i, r in enumerate(results) if r is None]
    if not valid_indices:
        return [{'status': 'success', 'output_image': None, 'image_id': os.path.basename(p)} for p in image_paths]

    files = []
    for i in valid_indices:
        path = image_paths[i]
        with open(path, 'rb') as f:
            files.append(('images', (os.path.basename(path), f.read(), 'image/png')))
    config_str = json.dumps({
        "render": {
            "font_scale_factor": 3.0,
            "line_spacing_ratio": 1.3,
            "text_padding_ratio": 1.0,
            "font_size_offset": 15,
            "font_size": 100
        },
        "text_merge": {"enabled": True},
        "mask_expand_ratio": 0.05
    })
    metadata_str = json.dumps(ocr_metadata)

    try:
        resp = requests.post(RETOUCH_DIRECT_URL,
                             files=files,
                             data={'config': config_str, 'ocr_metadata': metadata_str},
                             timeout=120)
        if resp.status_code != 200:
            err = f"修图服务 HTTP {resp.status_code}"
            logger.error(f"[远程修图] {err}")
            for i in range(len(image_paths)):
                if results[i] is None:
                    results[i] = {'status': 'failed', 'error': err, 'image_id': os.path.basename(image_paths[i])}
            return results

        data = resp.json()
        remote_results = data.get('data', {}).get('results', [])
        name_to_path = {}
        for i in range(len(image_paths)):
            name_to_path[os.path.basename(image_paths[i])] = i
        for rr in remote_results:
            img_name = rr.get('image_name', '')
            idx = name_to_path.get(img_name, -1)
            if 0 <= idx < len(results):
                results[idx] = {
                    'status': rr.get('status', 'failed'),
                    'output_image': rr.get('output_image'),
                    'image_id': img_name,
                    'error': rr.get('error', '')
                }
        for i, r in enumerate(results):
            if r is None:
                results[i] = {'status': 'failed', 'error': '无修图结果', 'image_id': os.path.basename(image_paths[i])}
        return results
    except Exception as e:
        logger.error(f"[远程修图] 异常: {e}")
        return [{'status': 'failed', 'error': str(e)[:200], 'image_id': os.path.basename(p)} for p in image_paths]


def call_local_retouch_aot_service(image_paths, ocr_metadata, batch_index, retouch_config, images_batch=None):
    """AOT-GAN 擦除 + Pillow 渲染译文，无 torch 时走 HTTP 远程"""
    # 无 torch → HTTP 远程修图
    if not _HAS_TORCH:
        return _retouch_via_http(image_paths, images_batch or [], ocr_metadata, retouch_config)

    # 以下为本地 AOT-GAN 路径（Windows 开发有 GPU）
    try:
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
                logger.error(f"[AOT修图] 图片读取失败 {img_basename}: {e}")
                results_data.append({'status': 'failed', 'error': f'图片读取失败: {e}', 'image_id': img_basename})
                continue

            h, w = img_cv_bgr.shape[:2]

            # AOT-GAN 擦除
            ratio = retouch_config.get('mask_expand_ratio', 0.1)
            mask = create_mask_from_blocks(h, w, blocks, expand_ratio=ratio)
            has_mask = np.sum(mask > 0) > 0
            if not has_mask:
                cvimg = img_cv_bgr.copy()
                results_data.append({'status': 'success', 'output_image': None, 'image_id': img_basename})
                continue
            aot_ok, aot_result = simple_inpaint_aot(path, mask)
            if aot_ok:
                cvimg = aot_result.copy()
                logger.info(f"[AOT修图] {img_basename}: AOT-GAN擦除成功")
            else:
                cvimg = cv2.inpaint(img_cv_bgr, mask, 3, cv2.INPAINT_TELEA)
                logger.info(f"[AOT修图] {img_basename}: AOT失败，回退OpenCV")

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
                except:
                    return ImageFont.load_default()

            # 碰撞合并（与原有逻辑一致）
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

            # 创建画布
            pil_i = Image.fromarray(cv2.cvtColor(cvimg, cv2.COLOR_BGR2RGB))
            draw = ImageDraw.Draw(pil_i)
            render_count = 0

            # 渲染（与原有逻辑一致，含填充）
            for blk in merged:
                txt = blk['text']
                x1, y1, x2, y2 = blk['x1'], blk['y1'], blk['x2'], blk['y2']
                # 框向外扩 20%（各边 10%）
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
                cx, cy = (x1 + x2) // 2, (y1 + y2) // 2


                fs = blk['font_size']
                if not isinstance(fs, (int, float)) or fs <= 10:
                    fs = max(12, int(bh * 0.85))
                fs = max(fs, 14)

                # 文字色、描边色（描边根据文字亮度反色）
                tc = blk.get('text_color', {})
                if isinstance(tc, dict):
                    fg = tc.get('fg', [0, 0, 0])
                    text_color = tuple(fg) if len(fg) == 3 else (0, 0, 0)
                else:
                    text_color = (0, 0, 0)
                fg_bright = (text_color[0] + text_color[1] + text_color[2]) // 3
                stroke_color = (0, 0, 0) if fg_bright > 128 else (255, 255, 255)

                fill_color = None

                pad = max(4, int(fs * 0.15))
                avail_w = max(10, bw - pad * 2)

                if vertical:
                    has_spaces = ' ' in txt.strip()
                    if has_spaces:
                        words = [w for w in txt.split(' ') if w]
                        min_fs = max(10, int(fs * 0.6))
                        sf = fs
                        fnt = _mkf(sf)
                        while sf >= min_fs:
                            fnt = _mkf(sf)
                            rh = draw.textbbox((0, 0), 'Ay', font=fnt)[3] + int(sf * 0.2)
                            ppc = max(1, int((bh - pad * 2) / rh))
                            mww = max(draw.textbbox((0, 0), w, font=fnt)[2] for w in words)
                            if ppc >= len(words) and mww <= avail_w:
                                break
                            sf -= 1
                        if fill_color:
                            fp2 = max(8, int(fs * 0.45))
                            draw.rectangle([x1 - fp2, y1 - fp2, x2 + fp2, y2 + fp2], fill=fill_color)
                        row_h = draw.textbbox((0, 0), 'Ay', font=fnt)[3] + int(sf * 0.2)
                        start_y = cy - (len(words) * row_h) // 2
                        for wi, wd in enumerate(words):
                            ww = draw.textbbox((0, 0), wd, font=fnt)[2]
                            draw.text((cx - ww // 2, start_y + wi * row_h), wd, font=fnt, fill=text_color, stroke_width=2, stroke_fill=stroke_color)
                    else:
                        # 没有空格：如果能横排放下就不竖排拆字
                        fnt_single = _mkf(fs)
                        tw_single = draw.textbbox((0, 0), txt, font=fnt_single)[2]
                        if tw_single <= avail_w:
                            sf = fs
                            fnt = fnt_single
                            if fill_color:
                                fp2 = max(8, int(fs * 0.45))
                                draw.rectangle([x1 - fp2, y1 - fp2, x2 + fp2, y2 + fp2], fill=fill_color)
                            line_h = draw.textbbox((0, 0), 'Ay', font=fnt)[3] + int(sf * 0.2)
                            draw.text((cx - tw_single // 2, cy - line_h // 2), txt, font=fnt,
                                      fill=text_color, stroke_width=2, stroke_fill=stroke_color)
                        else:
                            # 横排放不下才逐字竖排
                            def _vfits(sz):
                                f = _mkf(sz)
                                t = ImageDraw.Draw(Image.new('RGB', (1, 1)))
                                cw = t.textbbox((0, 0), '呵', font=f)[2] + int(sz * 0.15)
                                rh = t.textbbox((0, 0), '呵', font=f)[3] + int(sz * 0.15)
                                c = max(1, int((bh - pad * 2) / rh))
                                n = (len(txt) + c - 1) // c
                                return n * cw <= bw
                            low, high = max(10, int(fs * 0.7)), fs
                            while low < high:
                                mid = (low + high + 1) // 2
                                if _vfits(mid): low = mid
                                else: high = mid - 1
                            sf = low
                            fnt = _mkf(sf)
                            col_w = draw.textbbox((0, 0), '呵', font=fnt)[2] + int(sf * 0.15)
                            row_h = draw.textbbox((0, 0), '呵', font=fnt)[3] + int(sf * 0.15)
                            cpc = max(1, int((bh - pad * 2) / row_h))
                            need_cols = (len(txt) + cpc - 1) // cpc
                            total_w = need_cols * col_w
                            total_h = min(len(txt), cpc) * row_h
                            start_x = cx - total_w // 2
                            start_y = cy - total_h // 2
                            if fill_color:
                                fp2 = max(8, int(fs * 0.45))
                                draw.rectangle([x1 - fp2, y1 - fp2, x2 + fp2, y2 + fp2], fill=fill_color)
                            for ci, ch in enumerate(txt):
                                if ch.strip():
                                    draw.text((start_x + (ci // cpc) * col_w, start_y + (ci % cpc) * row_h),
                                              ch, font=fnt, fill=text_color, stroke_width=2, stroke_fill=stroke_color)
                else:
                    min_fs_h = max(10, int(fs * 0.8))
                    sf = fs
                    fnt = _mkf(sf)
                    while sf >= min_fs_h:
                        fnt = _mkf(sf)
                        if draw.textbbox((0, 0), txt, font=fnt)[2] <= avail_w:
                            break
                        sf -= 1
                    if fill_color:
                        fp2 = max(8, int(fs * 0.2))
                        draw.rectangle([x1 - fp2, y1 - fp2, x2 + fp2, y2 + fp2], fill=fill_color)
                    line_h = draw.textbbox((0, 0), 'Ay', font=fnt)[3] + int(sf * 0.2)
                    tw = draw.textbbox((0, 0), txt, font=fnt)[2]
                    if tw <= avail_w:
                        draw.text((cx - tw // 2, cy - line_h // 2), txt, font=fnt, fill=text_color, stroke_width=2, stroke_fill=stroke_color)
                    else:
                        lines, cur = [], ''
                        if ' ' in txt.strip():
                            words = txt.split(' ')
                            for wd in words:
                                test = (cur + ' ' + wd).strip() if cur else wd
                                if draw.textbbox((0, 0), test, font=fnt)[2] <= avail_w:
                                    cur = test
                                else:
                                    if cur: lines.append(cur)
                                    cur = wd
                        else:
                            for ch in txt:
                                test = cur + ch
                                if draw.textbbox((0, 0), test, font=fnt)[2] <= avail_w:
                                    cur = test
                                else:
                                    if cur: lines.append(cur)
                                    cur = ch
                        if cur: lines.append(cur)
                        if not lines: lines = [txt]
                        start_y = cy - (len(lines) * line_h) // 2
                        for li, ln in enumerate(lines):
                            lw = draw.textbbox((0, 0), ln, font=fnt)[2]
                            draw.text((cx - lw // 2, start_y + li * line_h), ln, font=fnt, fill=text_color, stroke_width=2, stroke_fill=stroke_color)

                render_count += 1

            cvimg = cv2.cvtColor(np.array(pil_i), cv2.COLOR_RGB2BGR)
            ok_b, buf = cv2.imencode('.webp', cvimg, [cv2.IMWRITE_WEBP_QUALITY, 85])
            if ok_b:
                b64 = base64.b64encode(buf.tobytes()).decode('utf-8')
                logger.info(f"[AOT修图] 第{batch_index}批图片{idx} {img_basename}: 渲染{render_count}个区块")
                results_data.append({'status': 'success', 'output_image': b64, 'image_id': img_basename})
            else:
                results_data.append({'status': 'failed', 'error': '编码失败', 'image_id': img_basename})

        return results_data
    except Exception as e:
        logger.error(f"[AOT修图] 第{batch_index}批调用失败: {str(e)}")
        traceback.print_exc()
        return []
