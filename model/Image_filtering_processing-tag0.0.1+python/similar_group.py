#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
相似图分组 + 模糊图片排除 + XLSX 报表 + 后处理（备份/删模糊/组内保留）

后处理顺序（每个文件夹）：
    1. 分析分组
    2. 写 <文件夹名>-样本分组展示.xlsx   （此时原图还在，能嵌入）
    3. 备份 zip                         （此时原图还在）
    4. 删模糊（若启用）
    5. 组内随机保留 N 张（若启用）
    6. 写 work.yaml（含本次后处理记录）

用法:
    python similar_group.py --input <文件夹> [--config config.yaml]
    python similar_group.py --input <文件夹> --output all_in_one.yaml

依赖:
    pip install pillow numpy pyyaml opencv-python scikit-image openpyxl
"""

import os
import re
import sys
import glob
import yaml
import random
import fnmatch
import zipfile
import argparse
from io import BytesIO
from datetime import datetime

import numpy as np
from PIL import Image
import cv2

try:
    from skimage.metrics import structural_similarity as ssim_fn
    HAS_SSIM = True
except ImportError:
    HAS_SSIM = False


# ---------------- 通用工具 ----------------

def natural_key(s):
    return [int(t) if t.isdigit() else t.lower()
            for t in re.split(r'(\d+)', str(s))]


def resolve_tmpl(tmpl, folder_name):
    """替换 {folder_name} 占位符；没有占位符则原样返回"""
    if not tmpl:
        return tmpl
    return tmpl.replace('{folder_name}', folder_name)


def load_config(path):
    if not os.path.exists(path):
        print(f'[提示] 未找到 {path}，使用内置默认配置')
        return {}
    with open(path, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f) or {}


def merge_defaults(cfg):
    defaults = {
        'similarity': {
            'method': 'multi_feature',
            'color_hist_bins': 32,
            'gradient_size': 48,
            'use_ssim': True,
            'ssim_size': 96,
            'dhash_size': 8,
            'weight_color': 0.45,
            'weight_gradient': 0.45,
            'weight_ssim': 0.10,
            'low_threshold': 0.30,
            'combined_threshold': 0.55,
            'high_score_bypass': 0.80,
            'color_min_corr': 0.7,
            'gradient_min_corr': 0.5,
            'ssim_min_score': 0.35,
            'dhash_veto_distance': 28,
            'strict_mode': True,
            'layout_size': 24,
            'layout_min_corr': 0.6,
            'hash_size': 8,
            'hash_max_distance': 10,
            'histogram_min_corr': 0.85,
        },
        'grouping': {
            'compare_mode': 'anchor',
            'max_consecutive_dissimilar': 2,
            'random_second_compare': False,
            'random_seed': 42,
            'pair_decision_threshold': 0.5,
        },
        'blur': {
            'enabled': True,
            'laplacian_threshold': 60.0,
            'skip_blurry': True,
            'count_as_dissimilar': False,
        },
        'scan': {
            'recursive': True,
            'process_root': True,
            'natural_sort': True,
            'extensions': ['.png', '.jpg', '.jpeg', '.bmp', '.webp', '.tif', '.tiff'],
            'folder_name_patterns': [],
            'folder_name_exclude': [],
        },
        'output': {
            'filename': 'work.yaml',
            'overwrite': True,
            'xlsx_enabled': True,
            'xlsx_filename': '{folder_name}-样本分组展示.xlsx',
            'xlsx_image_height_px': 80,
            'xlsx_max_name_col_width': 80,
            'xlsx_cell_padding_px': 8,
        },
        'post_process': {
            'backup_zip': False,
            'backup_zip_filename': '{folder_name}_backup.zip',
            'fuzzy_removal': False,
            'same_category_group_quantity_retention': 0,
            'additional_automatic_folder_naming_processing': False,
            'settings': {},
        },
    }
    for k, v in defaults.items():
        if k not in cfg or cfg[k] is None:
            cfg[k] = v
        else:
            for kk, vv in v.items():
                cfg[k].setdefault(kk, vv)
    return cfg


# ---------------- 特征提取 ----------------

class Feature:
    __slots__ = ('path', 'name', 'color_hist', 'gradient', 'ssim_gray',
                 'dhash', 'lap_var', 'is_blurry', 'valid', 'error')

    def __init__(self, path, name):
        self.path = path
        self.name = name
        self.color_hist = None
        self.gradient = None
        self.ssim_gray = None
        self.dhash = None
        self.lap_var = 0.0
        self.is_blurry = False
        self.valid = True
        self.error = None


def compute_color_hist(pil_img, bins):
    rgb = pil_img.convert('RGB')
    arr = np.asarray(rgb, dtype=np.uint8)
    parts = []
    for c in range(3):
        h, _ = np.histogram(arr[:, :, c], bins=bins, range=(0, 256))
        h = h.astype(np.float64)
        s = h.sum()
        parts.append(h / s if s > 0 else h)
    return np.concatenate(parts)


def compute_gradient(pil_img, size):
    img = pil_img.convert('L').resize((size, size), Image.LANCZOS)
    arr = np.asarray(img, dtype=np.float32)
    gx = cv2.Sobel(arr, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(arr, cv2.CV_32F, 0, 1, ksize=3)
    mag = np.sqrt(gx * gx + gy * gy)
    std = float(mag.std())
    if std < 1e-6:
        std = 1.0
    return (mag - mag.mean()) / std


def compute_ssim_gray(pil_img, size):
    img = pil_img.convert('L').resize((size, size), Image.LANCZOS)
    return np.asarray(img, dtype=np.uint8)


def compute_dhash(pil_img, hash_size):
    img = pil_img.convert('L').resize((hash_size + 1, hash_size), Image.LANCZOS)
    arr = np.asarray(img, dtype=np.int32)
    return (arr[:, 1:] > arr[:, :-1]).flatten()


def laplacian_var(pil_img):
    arr = np.asarray(pil_img.convert('L'))
    return float(cv2.Laplacian(arr, cv2.CV_64F).var())


def extract_feature(path, name, cfg):
    feat = Feature(path, name)
    sim = cfg['similarity']
    try:
        with Image.open(path) as im:
            im.load()
            feat.color_hist = compute_color_hist(im, int(sim['color_hist_bins']))
            feat.gradient = compute_gradient(im, int(sim['gradient_size']))
            feat.dhash = compute_dhash(im, int(sim['dhash_size']))
            if sim.get('use_ssim', True) and HAS_SSIM:
                feat.ssim_gray = compute_ssim_gray(im, int(sim['ssim_size']))
            if cfg['blur']['enabled']:
                feat.lap_var = laplacian_var(im)
                feat.is_blurry = feat.lap_var < float(cfg['blur']['laplacian_threshold'])
    except Exception as e:
        feat.valid = False
        feat.error = f'{type(e).__name__}: {e}'
    return feat


# ---------------- 相似度 ----------------

def color_corr(h1, h2):
    if h1 is None or h2 is None or h1.shape != h2.shape:
        return 0.0
    if h1.std() < 1e-9 or h2.std() < 1e-9:
        return 0.0
    return float(np.corrcoef(h1, h2)[0, 1])


def gradient_ncc(g1, g2):
    if g1 is None or g2 is None or g1.shape != g2.shape:
        return 0.0
    return float(np.mean(g1 * g2))


def ssim_score(g1, g2):
    if not HAS_SSIM or g1 is None or g2 is None or g1.shape != g2.shape:
        return 0.0
    try:
        return float(ssim_fn(g1, g2, data_range=255))
    except Exception:
        return 0.0


def pair_similarity(f1, f2, cfg):
    sim = cfg['similarity']
    res = {'with': f2.name}

    cc = color_corr(f1.color_hist, f2.color_hist)
    gn = gradient_ncc(f1.gradient, f2.gradient)
    ssim_avail = HAS_SSIM and f1.ssim_gray is not None and f2.ssim_gray is not None
    ss = ssim_score(f1.ssim_gray, f2.ssim_gray) if ssim_avail else 0.0
    dhash_dist = (int(np.sum(f1.dhash != f2.dhash))
                  if f1.dhash is not None and f2.dhash is not None else None)

    res['dhash_dist'] = dhash_dist
    res['color_corr'] = round(cc, 4)
    res['grad_ncc'] = round(gn, 4)
    res['ssim'] = round(ss, 4)

    w_c = float(sim['weight_color'])
    w_g = float(sim['weight_gradient'])
    w_s = float(sim['weight_ssim'])
    if not ssim_avail:
        total = w_c + w_g
        w_c, w_g, w_s = w_c / total, w_g / total, 0.0
    score = w_c * cc + w_g * gn + w_s * ss
    res['score'] = round(score, 4)

    low_thr = float(sim.get('low_threshold', 0.30))
    comb_thr = float(sim.get('combined_threshold', 0.55))
    high_bypass = float(sim.get('high_score_bypass', 0.80))

    if score >= high_bypass:
        ok, reason = True, 'score_high'
    elif score < low_thr:
        ok, reason = False, 'score_low'
    else:
        ok = score >= comb_thr
        reason = 'score_combined'
    res['reason'] = reason
    res['similar'] = ok
    return ok, res


# ---------------- 分组：锚点模式 ----------------

def group_features_anchor(features, cfg):
    skip_blurry = cfg['blur']['skip_blurry']
    active = [f for f in features
              if f.valid and not (skip_blurry and f.is_blurry)]
    if not active:
        return []

    groups = []
    current_members = [active[0]]
    current_details = []
    anchor = active[0]

    for i in range(1, len(active)):
        f = active[i]
        ok, detail = pair_similarity(f, anchor, cfg)

        if ok:
            current_members.append(f)
            current_details.append({
                'image': f.name,
                'avg': 1.0,
                'similar': True,
                'pairs': [detail],
            })
        else:
            groups.append({'members': current_members, 'details': current_details})
            current_members = [f]
            current_details = []
            anchor = f

    if current_members:
        groups.append({'members': current_members, 'details': current_details})

    return groups


# ---------------- 分组：链式 + 随机（兼容） ----------------

def group_features_chain(features, cfg):
    g = cfg['grouping']
    max_dis = int(g['max_consecutive_dissimilar'])
    pair_thr = float(g.get('pair_decision_threshold', 0.5))
    do_second = bool(g.get('random_second_compare', True))
    seed = g.get('random_seed', None)
    rng = random.Random(seed)
    skip_blurry = cfg['blur']['skip_blurry']

    active = [f for f in features
              if f.valid and not (skip_blurry and f.is_blurry)]
    if not active:
        return []

    groups = []
    cur_members = [active[0]]
    cur_details = []
    consecutive_dis = 0
    dis_start_idx = None

    for i in range(1, len(active)):
        f = active[i]
        last = cur_members[-1]

        ok1, d1 = pair_similarity(f, last, cfg)
        pair_results = [ok1]
        pair_details = [d1]

        if do_second and len(cur_members) >= 2:
            candidates = cur_members[:-1]
            other = rng.choice(candidates)
            ok2, d2 = pair_similarity(f, other, cfg)
            pair_results.append(ok2)
            pair_details.append(d2)

        avg = sum(1 for x in pair_results if x) / len(pair_results)
        similar = avg >= pair_thr

        cur_members.append(f)
        cur_details.append({
            'image': f.name,
            'avg': round(avg, 4),
            'similar': similar,
            'pairs': pair_details,
        })

        if similar:
            consecutive_dis = 0
            dis_start_idx = None
        else:
            if consecutive_dis == 0:
                dis_start_idx = len(cur_members) - 1
            consecutive_dis += 1
            if consecutive_dis >= max_dis:
                old_members = cur_members[:dis_start_idx]
                new_members = cur_members[dis_start_idx:]
                old_details = cur_details[:max(0, dis_start_idx - 1)]
                new_details = cur_details[dis_start_idx:]
                if old_members:
                    groups.append({'members': old_members, 'details': old_details})
                cur_members = list(new_members)
                cur_details = list(new_details)
                consecutive_dis = 0
                dis_start_idx = None

    if cur_members:
        groups.append({'members': cur_members, 'details': cur_details})

    return groups


def group_features(features, cfg):
    mode = cfg['grouping'].get('compare_mode', 'anchor')
    if mode == 'chain_with_random':
        return group_features_chain(features, cfg)
    return group_features_anchor(features, cfg)


# ---------------- 目录扫描 ----------------

def folder_matches(name, include_pats, exclude_pats):
    if exclude_pats:
        for p in exclude_pats:
            if fnmatch.fnmatch(name, p):
                return False
    if not include_pats:
        return True
    return any(fnmatch.fnmatch(name, p) for p in include_pats)


def collect_image_files(root, cfg):
    exts = {e.lower() for e in cfg['scan']['extensions']}
    recursive = bool(cfg['scan']['recursive'])
    process_root = bool(cfg['scan']['process_root'])
    include = cfg['scan']['folder_name_patterns'] or []
    exclude = cfg['scan']['folder_name_exclude'] or []
    use_natural = bool(cfg['scan']['natural_sort'])

    result = {}

    def gather(folder_path):
        try:
            names = os.listdir(folder_path)
        except OSError as e:
            print(f'[警告] 无法读取 {folder_path}: {e}')
            return
        files = []
        for n in names:
            full = os.path.join(folder_path, n)
            if os.path.isfile(full) and os.path.splitext(n)[1].lower() in exts:
                files.append(full)
        if not files:
            return
        files.sort(key=natural_key if use_natural else None)
        result[folder_path] = files

    if recursive:
        for dirpath, dirnames, _ in os.walk(root):
            dirnames[:] = [d for d in dirnames
                           if folder_matches(d, include, exclude)]
            if os.path.abspath(dirpath) == os.path.abspath(root):
                if process_root:
                    gather(dirpath)
                continue
            if folder_matches(os.path.basename(dirpath), include, exclude):
                gather(dirpath)
    else:
        if process_root:
            gather(root)
    return result


# ---------------- 清理旧文件 ----------------

def clean_previous_report(folder_path, filenames):
    for filename in filenames:
        target = os.path.join(folder_path, filename)
        if not os.path.isfile(target):
            continue
        if filename.lower().endswith(('.yaml', '.yml')):
            try:
                with open(target, 'r', encoding='utf-8') as f:
                    head = f.read(4096)
                if 'generated_at:' not in head:
                    print(f'         [跳过] {target} 非本脚本生成，未删除')
                    continue
            except Exception as e:
                print(f'         [跳过] 读取 {target} 失败: {e}')
                continue
        try:
            os.remove(target)
            print(f'         [清理] 已删除旧文件 {target}')
        except Exception as e:
            print(f'[警告] 删除 {target} 失败: {e}')


def clean_previous_xlsx(folder_path):
    """清理历史版本 xlsx（旧的固定名、动态名都清）"""
    patterns = [
        os.path.join(folder_path, 'work.xlsx'),
        os.path.join(folder_path, '*-样本分组展示.xlsx'),
    ]
    for pat in patterns:
        for p in glob.glob(pat):
            try:
                os.remove(p)
                print(f'         [清理] 已删除旧文件 {p}')
            except Exception as e:
                print(f'[警告] 删除 {p} 失败: {e}')


# ---------------- 分析 ----------------

def process_one_folder(folder_path, files, cfg):
    features = [extract_feature(fp, os.path.basename(fp), cfg) for fp in files]
    groups = group_features(features, cfg)

    folder_out = {
        'path': os.path.abspath(folder_path),
        'image_count': len(features),
        'groups': [],
        'images': {},
    }

    grouped_names = set()
    for gi, grp in enumerate(groups, 1):
        member_names = [m.name for m in grp['members']]
        folder_out['groups'].append({
            'group_id': gi,
            'size': len(member_names),
            'images': member_names,
            'details': grp['details'],
        })
        for m in grp['members']:
            grouped_names.add(m.name)
            folder_out['images'][m.name] = {
                'group': gi,
                'is_blurry': m.is_blurry,
                'lap_var': round(m.lap_var, 2),
                'valid': m.valid,
                'skipped': False,
            }

    for f in features:
        if f.name in grouped_names:
            continue
        reason = 'error' if not f.valid else ('blurry' if f.is_blurry else 'unknown')
        folder_out['images'][f.name] = {
            'group': None,
            'is_blurry': f.is_blurry,
            'lap_var': round(f.lap_var, 2),
            'valid': f.valid,
            'skipped': True,
            'skip_reason': reason,
            'error': f.error,
        }
    return folder_out


def write_yaml(path, data):
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        yaml.safe_dump(data, f, allow_unicode=True,
                       sort_keys=False, default_flow_style=False)
    os.replace(tmp, path)


# ---------------- XLSX ----------------

def _make_cell_thumb(path, canvas_w_px, canvas_h_px, target_h_px):
    with Image.open(path) as im:
        im2 = im.convert('RGB')
        w0, h0 = im2.size
        if h0 <= 0 or w0 <= 0:
            raise ValueError('图片尺寸无效')
        scale = target_h_px / float(h0)
        tw = max(1, int(round(w0 * scale)))
        th = max(1, int(round(target_h_px)))
        max_w = max(1, int(canvas_w_px) - 2)
        if tw > max_w:
            s2 = max_w / float(tw)
            tw = max(1, int(round(tw * s2)))
            th = max(1, int(round(th * s2)))
        thumb = im2.resize((tw, th), Image.LANCZOS)

        canvas = Image.new(
            'RGB',
            (max(1, int(round(canvas_w_px))), max(1, int(round(canvas_h_px)))),
            (255, 255, 255),
        )
        ox = (canvas.width - tw) // 2
        oy = (canvas.height - th) // 2
        canvas.paste(thumb, (ox, oy))

        bio = BytesIO()
        canvas.save(bio, format='PNG')
        bio.seek(0)
        return bio, canvas.width, canvas.height


def write_xlsx(folder_path, xlsx_name, out, cfg):
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, Alignment, Border, Side
        from openpyxl.drawing.image import Image as XLImage
    except ImportError:
        print('         [跳过 xlsx] 未安装 openpyxl，请执行: pip install openpyxl')
        return False

    target_h_px = float(cfg['output'].get('xlsx_image_height_px', 80))
    max_name_w = int(cfg['output'].get('xlsx_max_name_col_width', 80))
    pad_px = float(cfg['output'].get('xlsx_cell_padding_px', 8))

    records = []
    for grp in out['groups']:
        gid = grp['group_id']
        for name in grp['images']:
            records.append((gid, name, os.path.join(folder_path, name)))
    if not records:
        return False

    max_thumb_w = 0.0
    for _g, _n, p in records:
        try:
            with Image.open(p) as im:
                w0, h0 = im.size
            if h0 > 0:
                w = w0 * (target_h_px / float(h0))
                max_thumb_w = max(max_thumb_w, w)
        except Exception:
            pass
    cell_w_px = max(max_thumb_w + pad_px, 80.0)
    cell_h_px = target_h_px + pad_px

    col_c_width = max((cell_w_px - 5.0) / 7.0, 10.0)
    row_h_pt = cell_h_px * 72.0 / 96.0

    max_name_len = max(len(r[1]) for r in records)
    col_b_width = min(max(max_name_len + 2, 15), max_name_w)

    wb = Workbook()
    ws = wb.active
    ws.title = '分组'

    thin = Side(style='thin', color='FF000000')
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    center = Alignment(horizontal='center', vertical='center', wrap_text=True)
    header_font = Font(bold=True)

    for col, h in enumerate(['组号', '图片名', '图片'], 1):
        c = ws.cell(row=1, column=col, value=h)
        c.font = header_font
        c.alignment = center
        c.border = border
    ws.row_dimensions[1].height = 22

    ws.column_dimensions['A'].width = 8
    ws.column_dimensions['B'].width = col_b_width
    ws.column_dimensions['C'].width = col_c_width

    row = 2
    group_ranges = []
    cur_gid, cur_start = None, None

    for gid, name, path in records:
        if gid != cur_gid:
            if cur_gid is not None:
                group_ranges.append((cur_gid, cur_start, row - 1))
            cur_gid, cur_start = gid, row

        ca = ws.cell(row=row, column=1, value=gid)
        cb = ws.cell(row=row, column=2, value=name)
        cc = ws.cell(row=row, column=3, value=None)
        ca.alignment = center; cb.alignment = center; cc.alignment = center
        ca.border = border; cb.border = border; cc.border = border

        try:
            bio, cw, ch = _make_cell_thumb(path, cell_w_px, cell_h_px, target_h_px)
            ximg = XLImage(bio)
            ximg.width = cw
            ximg.height = ch
            ws.add_image(ximg, f'C{row}')
        except Exception as e:
            cc.value = f'[图片加载失败] {e}'

        ws.row_dimensions[row].height = row_h_pt
        row += 1

    if cur_gid is not None:
        group_ranges.append((cur_gid, cur_start, row - 1))

    for _g, s, e in group_ranges:
        if e > s:
            ws.merge_cells(start_row=s, start_column=1, end_row=e, end_column=1)

    ws.freeze_panes = 'A2'

    target = os.path.join(folder_path, xlsx_name)
    tmp = target + '.tmp'
    wb.save(tmp)
    os.replace(tmp, target)
    return True


# ---------------- 后处理：规则匹配 ----------------

def resolve_post_rules(folder_path, cfg):
    """返回最终生效的规则字典（setting 优先于全局）"""
    pp = cfg['post_process']
    rules = {
        'backup_zip': bool(pp.get('backup_zip', False)),
        'backup_zip_filename': pp.get('backup_zip_filename', '{folder_name}_backup.zip'),
        'fuzzy_removal': bool(pp.get('fuzzy_removal', False)),
        'same_category_group_quantity_retention': int(
            pp.get('same_category_group_quantity_retention', 0) or 0),
        'matched_setting': None,
    }

    if not pp.get('additional_automatic_folder_naming_processing', False):
        return rules

    settings = pp.get('settings', {}) or {}
    if not isinstance(settings, dict):
        return rules

    folder_name = os.path.basename(os.path.abspath(folder_path))

    # 1) 精确匹配
    matched = None
    for sname, s in settings.items():
        if not isinstance(s, dict):
            continue
        folders = s.get('folders', []) or []
        if folder_name in folders:
            matched = (sname, s)
            break

    # 2) 子串匹配
    if matched is None:
        for sname, s in settings.items():
            if not isinstance(s, dict):
                continue
            folders = s.get('folders', []) or []
            for f in folders:
                if f and f in folder_name:
                    matched = (sname, s)
                    break
            if matched:
                break

    if matched is not None:
        sname, s = matched
        rules['matched_setting'] = sname
        if 'fuzzy_removal' in s:
            rules['fuzzy_removal'] = bool(s['fuzzy_removal'])
        if 'same_category_group_quantity_retention' in s:
            try:
                rules['same_category_group_quantity_retention'] = int(
                    s['same_category_group_quantity_retention'] or 0)
            except (TypeError, ValueError):
                pass
    return rules


# ---------------- 后处理：备份 ----------------

def do_backup_zip(folder_path, files, rules):
    """把 files 中的图片打包到 folder_path/{folder_name}_backup.zip"""
    folder_name = os.path.basename(os.path.abspath(folder_path))
    zip_name = resolve_tmpl(rules['backup_zip_filename'], folder_name)
    if not zip_name:
        zip_name = f'{folder_name}_backup.zip'
    zip_path = os.path.join(folder_path, zip_name)

    if os.path.isfile(zip_path):
        try:
            os.remove(zip_path)
        except Exception as e:
            print(f'[警告] 无法覆盖旧备份 {zip_path}: {e}')

    try:
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            for fp in files:
                if os.path.isfile(fp):
                    zf.write(fp, arcname=os.path.basename(fp))
        return zip_name
    except Exception as e:
        print(f'[警告] 备份失败 {zip_path}: {e}')
        return None


# ---------------- 后处理：删模糊 ----------------

def delete_blurry(folder_path, out):
    """删除 is_blurry 且 valid 的图片，返回被删列表"""
    deleted = []
    for name, info in out['images'].items():
        if not info.get('is_blurry') or not info.get('valid', True):
            continue
        fp = os.path.join(folder_path, name)
        if not os.path.isfile(fp):
            continue
        try:
            os.remove(fp)
            deleted.append(name)
        except Exception as e:
            print(f'[警告] 删除模糊图 {fp} 失败: {e}')
    return deleted


# ---------------- 后处理：组内保留数量 ----------------

def apply_retention(folder_path, out, retention, seed):
    """每组最多保留 retention 张；超出则随机保留。
    返回 (deleted_list, kept_dict)
    """
    if retention is None or retention <= 0:
        return [], {}

    rng = random.Random(seed)
    deleted = []
    kept = {}

    for grp in out['groups']:
        gid = grp['group_id']
        # 只处理当前磁盘上仍存在的图片
        existing = [n for n in grp['images']
                    if os.path.isfile(os.path.join(folder_path, n))]

        if len(existing) <= retention:
            kept[gid] = existing
            continue

        keep_set = set(rng.sample(existing, retention))
        kept[gid] = sorted(keep_set)

        for n in existing:
            if n in keep_set:
                continue
            fp = os.path.join(folder_path, n)
            try:
                os.remove(fp)
                deleted.append({'image': n, 'group': gid})
            except Exception as e:
                print(f'[警告] 删除 {fp} 失败: {e}')

    return deleted, kept


# ---------------- 主流程 ----------------

def main():
    ap = argparse.ArgumentParser(description='相似图分组 + 后处理 + XLSX 报表')
    ap.add_argument('--input', '-i', required=True, help='待处理根目录')
    ap.add_argument('--config', '-c', default='config.yaml',
                    help='配置文件路径（默认脚本同目录 config.yaml）')
    ap.add_argument('--output', '-o', default=None,
                    help='可选：聚合模式，把所有结果写到单个 YAML；'
                         '不传则每个文件夹单独生成 work.yaml + xlsx + 备份 + 后处理')
    args = ap.parse_args()

    if not os.path.isdir(args.input):
        print(f'[错误] 目录不存在: {args.input}')
        sys.exit(1)

    cfg = merge_defaults(load_config(args.config))

    if cfg['similarity'].get('use_ssim', True) and not HAS_SSIM:
        print('[提示] 未安装 scikit-image，SSIM 已自动禁用。'
              '安装命令：pip install scikit-image')

    root = os.path.abspath(args.input)
    out_name = cfg['output'].get('filename', 'work.yaml')
    xlsx_enabled = bool(cfg['output'].get('xlsx_enabled', True))
    overwrite = bool(cfg['output'].get('overwrite', True))
    xlsx_tmpl = cfg['output'].get('xlsx_filename', '{folder_name}-样本分组展示.xlsx')
    seed = cfg['grouping'].get('random_seed', 42)

    files_map = collect_image_files(root, cfg)
    if not files_map:
        print('[提示] 未找到任何图片')
        sys.exit(0)

    print(f'[扫描] 共 {len(files_map)} 个文件夹含图片')
    print(f'[模式] {cfg["grouping"].get("compare_mode", "anchor")}')

    aggregated = {}
    written_yaml = 0
    written_xlsx = 0
    written_zip = 0
    total_deleted_blurry = 0
    total_deleted_retention = 0

    for folder_path in sorted(files_map.keys(), key=natural_key):
        files = files_map[folder_path]
        folder_name = os.path.basename(os.path.abspath(folder_path))
        print(f'[处理] {folder_path}  ({len(files)} 张)')

        # 聚合模式下不写文件、不做后处理
        if args.output:
            out = process_one_folder(folder_path, files, cfg)
            rel = os.path.relpath(folder_path, root).replace(os.sep, '/')
            if rel == '.':
                rel = folder_name or 'root'
            key = rel
            n = 1
            while key in aggregated:
                n += 1
                key = f'{rel}#{n}'
            aggregated[key] = out
            continue

        # 清理旧文件
        if overwrite:
            clean_previous_report(folder_path, [out_name])
            if xlsx_enabled:
                clean_previous_xlsx(folder_path)

        # 1) 分析
        out = process_one_folder(folder_path, files, cfg)

        # 2) 匹配后处理规则
        rules = resolve_post_rules(folder_path, cfg)
        if rules['matched_setting']:
            print(f'         [规则] 命中 setting={rules["matched_setting"]}  '
                  f'(fuzzy_removal={rules["fuzzy_removal"]}, '
                  f'retention={rules["same_category_group_quantity_retention"]})')
        else:
            print(f'         [规则] 使用全局  '
                  f'(fuzzy_removal={rules["fuzzy_removal"]}, '
                  f'retention={rules["same_category_group_quantity_retention"]})')

        # 3) 写 xlsx（此时原图还在）
        xlsx_target_name = resolve_tmpl(xlsx_tmpl, folder_name)
        if xlsx_enabled and xlsx_target_name:
            ok = write_xlsx(folder_path, xlsx_target_name, out, cfg)
            if ok:
                written_xlsx += 1
                print(f'         -> 写入 {os.path.join(folder_path, xlsx_target_name)}')

        # 4) 备份 zip（此时原图还在）
        backup_zip_name = None
        if rules['backup_zip']:
            backup_zip_name = do_backup_zip(folder_path, files, rules)
            if backup_zip_name:
                written_zip += 1
                print(f'         -> 备份 {os.path.join(folder_path, backup_zip_name)}')

        # 5) 删模糊
        deleted_blurry = []
        if rules['fuzzy_removal']:
            deleted_blurry = delete_blurry(folder_path, out)
            if deleted_blurry:
                total_deleted_blurry += len(deleted_blurry)
                print(f'         -> 删除模糊图 {len(deleted_blurry)} 张')

        # 6) 组内保留
        deleted_retention = []
        kept_retention = {}
        retention = rules['same_category_group_quantity_retention']
        if retention > 0:
            deleted_retention, kept_retention = apply_retention(
                folder_path, out, retention, seed)
            if deleted_retention:
                total_deleted_retention += len(deleted_retention)
                print(f'         -> 组内保留 {retention} 张，删除 {len(deleted_retention)} 张')

        # 7) 写 work.yaml（含后处理结果）
        doc = {
            'generated_at': datetime.now().isoformat(timespec='seconds'),
            'folder': os.path.abspath(folder_path),
            'source_root': root,
            'config_snapshot': cfg,
            'image_count': out['image_count'],
            'groups': out['groups'],
            'images': out['images'],
            'post_process_result': {
                'matched_setting': rules['matched_setting'],
                'fuzzy_removal': rules['fuzzy_removal'],
                'same_category_group_quantity_retention':
                    rules['same_category_group_quantity_retention'],
                'backup_zip': backup_zip_name,
                'deleted_blurry': deleted_blurry,
                'deleted_by_retention': deleted_retention,
                'kept_by_retention': kept_retention,
            },
        }
        target_yaml = os.path.join(folder_path, out_name)
        try:
            write_yaml(target_yaml, doc)
            written_yaml += 1
            print(f'         -> 写入 {target_yaml}')
        except Exception as e:
            print(f'[警告] 写入 {target_yaml} 失败: {e}')

    if args.output:
        doc = {
            'generated_at': datetime.now().isoformat(timespec='seconds'),
            'root': root,
            'config_snapshot': cfg,
            'folders': aggregated,
        }
        write_yaml(args.output, doc)
        print(f'[完成] 聚合结果写入 {os.path.abspath(args.output)}')
    else:
        print(f'[完成] yaml={written_yaml}  xlsx={written_xlsx}  zip={written_zip}')
        print(f'        删除模糊图={total_deleted_blurry}  '
              f'组内保留删除={total_deleted_retention}')


if __name__ == '__main__':
    main()