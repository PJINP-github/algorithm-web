#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import re
import ast
import sys
import os
import time
import matplotlib.pyplot as plt
import matplotlib.patches as patches

# ================= 可手动调整的配置参数 =================
SHOW_POINTER_END = True          # 是否显示指针末端圆点（True/False）
POINTER_END_MARKER_SIZE = 4      # 指针末端圆点大小（数字越小点越小）
POINTER_END_COLOR = 'purple'     # 指针末端圆点颜色
# ======================================================

# 设置中文字体（Windows 内置）
plt.rcParams['font.sans-serif'] = ['SimHei', 'Microsoft YaHei', 'Arial Unicode MS']
plt.rcParams['axes.unicode_minus'] = False

# ----------------------------------------------------------------------
# 解析函数（原有基础上新增中间信息提取）
# ----------------------------------------------------------------------
def parse_meter_records(text):
    """
    从日志文本中提取所有仪表盘检测记录。
    除了原有字段外，新增提取：
        final_value: 最终读数
        three_point_info: 三点法选中的三个点及其数值（全局坐标）
        angle_bisector_vector: 角平分线方向向量
        polar_sorted_points: 极角排序后的点列表（局部坐标）
        max_angle_pair: 最大夹角点索引对
        final_start_point: 最终确定的起始点（局部坐标）
        start_pointer_angle: 起始点到指针的角度
        adjacent_ids: 相邻点索引对
        prior_direction: 先验方向
        model_direction: 模型识别方向
        decision_text: 决策描述（可选）
    """
    records = []
    blocks = re.split(r'(?=>>>>\s*P1 crop:)', text)
    for block in blocks:
        if not block.strip():
            continue

        rec = {}

        # ----- 原有解析 -----
        crop_match = re.search(r'P1 crop:\s*\((\d+),\s*(\d+),\s*(\d+),\s*(\d+)\)', block)
        if not crop_match:
            continue
        rec['crop'] = tuple(map(int, crop_match.groups()))

        center_match = re.search(r'P1 center:\s*\((\d+),\s*(\d+)\)', block)
        rec['center'] = tuple(map(int, center_match.groups())) if center_match else None

        pointer_match = re.search(r'P2 pointer black:\s*\((\d+),\s*(\d+)\)', block)
        rec['pointer_local'] = tuple(map(int, pointer_match.groups())) if pointer_match else None

        marks_block = re.search(r'>>>>\s*detect marks:.*?(\[.*\])(?=\s*>>>>|\s*$)', block, re.DOTALL)
        all_marks = []
        if marks_block:
            try:
                all_marks = ast.literal_eval(marks_block.group(1))
            except Exception as e:
                print(f"解析 detect marks 失败: {e}")

        value_marks = []
        for item in all_marks:
            if isinstance(item, dict) and item.get('type') == 'valueCoords':
                point = item.get('point')
                value = item.get('value')
                if isinstance(point, (list, tuple)) and len(point) >= 2 and value is not None:
                    try:
                        value_marks.append({'point': (int(point[0]), int(point[1])), 'value': value})
                    except (ValueError, TypeError):
                        pass
        rec['value_marks'] = value_marks

        selected_marks = []
        sel_match = re.search(r'>>>>\s*selected recg marks:\s*(\[.*?\])', block, re.DOTALL)
        if sel_match:
            try:
                sel_list = ast.literal_eval(sel_match.group(1))
                for item in sel_list:
                    if isinstance(item, (list, tuple)) and len(item) >= 2:
                        selected_marks.append((int(item[0]), int(item[1])))
            except Exception as e:
                print(f"解析 selected recg marks 失败: {e}")
        rec['selected_recg_marks'] = selected_marks

        dir_match = re.search(r'(?:开口方向(?:是)?|模型识别出来的开口方向是)[:：]?\s*(\w+)', block)
        rec['direction'] = dir_match.group(1) if dir_match else None

        vec_match = re.search(r'方向向量:\s*\(dx=([\d.]+),\s*dy=([\d.]+)\)', block)
        rec['direction_vector'] = (float(vec_match.group(1)), float(vec_match.group(2))) if vec_match else None

        # ----- 新增中间信息提取 -----
        # final 值
        final_match = re.search(r'final:\s*([\d.]+)', block)
        rec['final_value'] = float(final_match.group(1)) if final_match else None

        # 三点法选中的点
        three_point_info = {}
        tp_min = re.search(r'最小值\s*([\d.]+)\s*的坐标:\s*\[(\d+),\s*(\d+)\]', block)
        tp_mid = re.search(r'中间值\s*([\d.]+)\s*的坐标:\s*\[(\d+),\s*(\d+)\]', block)
        tp_max = re.search(r'最大值\s*([\d.]+)\s*的坐标:\s*\[(\d+),\s*(\d+)\]', block)
        if tp_min:
            three_point_info['min'] = {'value': float(tp_min.group(1)), 'point': (int(tp_min.group(2)), int(tp_min.group(3)))}
        if tp_mid:
            three_point_info['mid'] = {'value': float(tp_mid.group(1)), 'point': (int(tp_mid.group(2)), int(tp_mid.group(3)))}
        if tp_max:
            three_point_info['max'] = {'value': float(tp_max.group(1)), 'point': (int(tp_max.group(2)), int(tp_max.group(3)))}
        rec['three_point_info'] = three_point_info if three_point_info else None

        # 角平分线方向向量
        bisector_match = re.search(r'角平分线方向向量:\s*\(dx=([\d.]+),\s*dy=([\d.]+)\)', block)
        rec['angle_bisector_vector'] = (float(bisector_match.group(1)), float(bisector_match.group(2))) if bisector_match else None

        # 极角排序点
        polar_sorted_match = re.search(r'polar sort mark:\s*(\[.*?\])', block, re.DOTALL)
        if polar_sorted_match:
            try:
                polar_list = ast.literal_eval(polar_sorted_match.group(1))
                rec['polar_sorted_points'] = [(int(p[0]), int(p[1])) for p in polar_list if isinstance(p, (list, tuple)) and len(p)>=2]
            except:
                rec['polar_sorted_points'] = []
        else:
            rec['polar_sorted_points'] = []

        # 最大夹角点索引对
        max_angle_match = re.search(r'max angle between:\s*\((-?\d+),\s*(\d+)\)', block)
        if max_angle_match:
            rec['max_angle_pair'] = (int(max_angle_match.group(1)), int(max_angle_match.group(2)))
        else:
            rec['max_angle_pair'] = None

        # 最终起始点（局部坐标）
        start_point_match = re.search(r'set start p @\s*\d+\s*:\s*\((\d+),\s*(\d+)\)', block)
        rec['final_start_point'] = (int(start_point_match.group(1)), int(start_point_match.group(2))) if start_point_match else None

        # 起始点到指针的角度
        ang_match = re.search(r'ang\(START_O_POINTER\):\s*([\d.]+)', block)
        rec['start_pointer_angle'] = float(ang_match.group(1)) if ang_match else None

        # 相邻点索引
        adj_match = re.search(r'adjacent ids:\s*\((\d+),\s*(\d+)\)', block)
        rec['adjacent_ids'] = (int(adj_match.group(1)), int(adj_match.group(2))) if adj_match else None

        # 先验方向
        prior_dir_match = re.search(r'通过标定点判断的开口方向是:\s*(\w+)', block)
        rec['prior_direction'] = prior_dir_match.group(1) if prior_dir_match else None

        # 模型方向
        model_dir_match = re.search(r'模型识别出来的开口方向是:\s*(\w+)', block)
        rec['model_direction'] = model_dir_match.group(1) if model_dir_match else None

        # 决策描述（简单提取）
        decision_match = re.search(r'>>> 决策：(.*?)\n', block)
        rec['decision_text'] = decision_match.group(1).strip() if decision_match else ''

        records.append(rec)

    return records

# ----------------------------------------------------------------------
# 绘制单个记录（新增分析可视化）
# ----------------------------------------------------------------------
def plot_meter_record(rec, title="仪表盘检测结果", save_path=None, show=False):
    crop = rec.get('crop')
    if not crop:
        print("无裁剪区域，跳过绘制")
        return
    x1, y1, x2, y2 = crop
    center = rec.get('center')
    pointer_local = rec.get('pointer_local')
    value_marks = rec.get('value_marks', [])
    selected_marks = rec.get('selected_recg_marks', [])
    direction = rec.get('direction')
    direction_vec = rec.get('direction_vector')

    # 新增提取的字段
    final_value = rec.get('final_value')
    three_point_info = rec.get('three_point_info')
    angle_bisector_vector = rec.get('angle_bisector_vector')
    polar_sorted_points = rec.get('polar_sorted_points', [])
    max_angle_pair = rec.get('max_angle_pair')
    final_start_point = rec.get('final_start_point')
    start_pointer_angle = rec.get('start_pointer_angle')
    adjacent_ids = rec.get('adjacent_ids')
    prior_direction = rec.get('prior_direction')
    model_direction = rec.get('model_direction')
    decision_text = rec.get('decision_text', '')

    # 计算全局指针坐标
    pointer_abs = None
    if pointer_local:
        px, py = pointer_local
        pointer_abs = (x1 + px, y1 + py)

    # 将局部坐标转换为全局坐标的辅助函数
    def local_to_global(point):
        if point is None:
            return None
        return (x1 + point[0], y1 + point[1])

    # 转换 selected_recg_marks 为全局
    selected_marks_global = [local_to_global(p) for p in selected_marks]
    # 转换 polar_sorted_points 为全局
    polar_sorted_global = [local_to_global(p) for p in polar_sorted_points]
    # 转换 final_start_point 为全局
    final_start_global = local_to_global(final_start_point)

    # 创建画布，加宽以容纳右侧文字
    fig, ax = plt.subplots(figsize=(14, 8))
    margin = 50

    # 设置坐标轴
    ax.set_xlim(x1 - margin, x2 + margin)
    ax.set_ylim(y1 - margin, y2 + margin)
    ax.invert_yaxis()
    ax.set_aspect('equal')
    ax.grid(True, linestyle='--', alpha=0.6)
    ax.set_xlabel("X 坐标 (像素) →")
    ax.set_ylabel("Y 坐标 (像素) ↓", rotation=0, labelpad=20)

    # 标题包含最终读数
    title_text = title
    if final_value is not None:
        title_text += f"  (最终读数: {final_value})"
    ax.set_title(title_text, fontsize=12)

    # 1. 裁剪框
    rect = patches.Rectangle((x1, y1), x2 - x1, y2 - y1,
                             linewidth=2, edgecolor='red', facecolor='none',
                             label='裁剪区域 (ROI)')
    ax.add_patch(rect)

    # 2. 仪表中心（红色小圆点）
    if center:
        ax.plot(center[0], center[1], 'ro', markersize=8, label='仪表中心')

    # 3. 指针末端及方向线
    if pointer_abs:
        if center:
            ax.annotate('', xy=pointer_abs, xytext=center,
                        arrowprops=dict(arrowstyle='->', color='blue', lw=2,
                                        shrinkA=5, shrinkB=5))
            ax.plot([], [], color='blue', lw=2, marker='>', label='指针指向')
        if SHOW_POINTER_END:
            ax.plot(pointer_abs[0], pointer_abs[1], 'o', color=POINTER_END_COLOR,
                    markersize=POINTER_END_MARKER_SIZE, label='指针末端')

    # 4. 刻度点（绿色方形）
    if value_marks:
        for i, vm in enumerate(value_marks):
            px, py = vm['point']
            val = vm['value']
            label = '刻度点（点标定）' if i == 0 else ""
            ax.plot(px, py, 'gs', markersize=8, label=label)
            ax.annotate(f"{val}", (px, py), textcoords="offset points",
                        xytext=(8, 8), fontsize=9, color='darkgreen', weight='bold')

    # 5. 备用识别点（蓝色圆点，仅当无有效刻度点时显示）
    if selected_marks_global and not value_marks:
        for i, (px, py) in enumerate(selected_marks_global):
            label = '备用识别点 (selected)' if i == 0 else ""
            ax.plot(px, py, 'bo', markersize=8, label=label)

    # 6. 开口方向箭头（拉长，至少为角平分线箭头长度的一半）
    if direction_vec and center:
        dx, dy = direction_vec
        # 角平分线箭头长度系数为200，此处开口方向箭头系数为100（一半）
        ax.arrow(center[0], center[1], dx * 100, dy * 100,
                 head_width=20, head_length=20, fc='orange', ec='orange',
                 label=f'开口方向: {direction}' if direction else '方向向量')

    # ========== 新增：绘制中间分析元素 ==========
    # 三点法选中的点（红色星形，连线）
    if three_point_info:
        pts = []
        for key in ['min', 'mid', 'max']:
            info = three_point_info.get(key)
            if info:
                p = info['point']
                pts.append(p)
                ax.plot(p[0], p[1], '*', color='red', markersize=12,
                        label='三点法点' if key == 'min' else "")
                ax.annotate(f"{key}={info['value']}", (p[0], p[1]),
                            textcoords="offset points", xytext=(5, -15),
                            fontsize=7, color='red')
        if len(pts) >= 2:
            ax.plot([p[0] for p in pts], [p[1] for p in pts], 'r--', alpha=0.5)

    # 角平分线向量（洋红色箭头）
    if angle_bisector_vector and center:
        dx, dy = angle_bisector_vector
        ax.arrow(center[0], center[1], dx * 200, dy * 200,
                 head_width=15, head_length=15, fc='magenta', ec='magenta',
                 alpha=0.7, label='角平分线方向')

    # 最终起始点（紫色X）
    if final_start_global:
        ax.plot(final_start_global[0], final_start_global[1], 'x', color='purple',
                markersize=10, markeredgewidth=2, label='最终起始点')

    # 极角排序点（小圆圈，淡蓝色）
    if polar_sorted_global:
        show_polar = polar_sorted_global[:6]
        for i, p in enumerate(show_polar):
            ax.plot(p[0], p[1], 'o', color='lightblue', markersize=6, alpha=0.7,
                    label='极角排序点' if i == 0 else "")

    # 标记最大夹角对
    if max_angle_pair and polar_sorted_global:
        idx1, idx2 = max_angle_pair
        if idx1 < 0:
            idx1 += len(polar_sorted_global)
        if idx2 < 0:
            idx2 += len(polar_sorted_global)
        if 0 <= idx1 < len(polar_sorted_global) and 0 <= idx2 < len(polar_sorted_global):
            p1 = polar_sorted_global[idx1]
            p2 = polar_sorted_global[idx2]
            ax.plot(p1[0], p1[1], 'D', color='orange', markersize=8, label='最大夹角点')
            ax.plot(p2[0], p2[1], 'D', color='orange', markersize=8)

    # 标记相邻点
    if adjacent_ids and polar_sorted_global:
        for idx in adjacent_ids:
            if 0 <= idx < len(polar_sorted_global):
                p = polar_sorted_global[idx]
                ax.plot(p[0], p[1], 's', color='cyan', markersize=7, alpha=0.8,
                        label='相邻点' if idx == adjacent_ids[0] else "")

    # ========== 右侧分析描述文本 ==========
    desc_lines = []
    desc_lines.append("=== 分析过程 ===")
    if final_value is not None:
        desc_lines.append(f"最终读数: {final_value}")
    if prior_direction:
        desc_lines.append(f"先验方向: {prior_direction}")
    if model_direction:
        desc_lines.append(f"模型方向: {model_direction}")
    if decision_text:
        desc_lines.append(f"决策: {decision_text}")
    if three_point_info:
        desc_lines.append("三点法:")
        for key in ['min', 'mid', 'max']:
            info = three_point_info.get(key)
            if info:
                desc_lines.append(f"  {key}: 值={info['value']}, 点=({info['point'][0]},{info['point'][1]})")
    if max_angle_pair:
        desc_lines.append(f"最大夹角点对: {max_angle_pair}")
    if angle_bisector_vector:
        desc_lines.append(f"角平分线向量: dx={angle_bisector_vector[0]:.2f}, dy={angle_bisector_vector[1]:.2f}")
    if final_start_global:
        desc_lines.append(f"最终起始点(全局): ({final_start_global[0]},{final_start_global[1]})")
    if start_pointer_angle is not None:
        desc_lines.append(f"起始-指针角度: {start_pointer_angle:.2f} rad")
    if adjacent_ids:
        desc_lines.append(f"相邻点索引: {adjacent_ids}")

    desc_text = "\n".join(desc_lines)
    # 移除 family='monospace'，使用全局中文字体
    fig.text(0.78, 0.5, desc_text, fontsize=9, verticalalignment='center',
             bbox=dict(boxstyle='round', facecolor='wheat', alpha=0.5))

    # 调整子图位置，给右侧留出空间
    plt.subplots_adjust(right=0.75)

    # 图例
    handles, labels = ax.get_legend_handles_labels()
    unique = dict(zip(labels, handles))
    if unique:
        ax.legend(unique.values(), unique.keys(), loc='upper left', fontsize=8)

    plt.tight_layout()

    if save_path:
        plt.savefig(save_path, dpi=150, bbox_inches='tight')
        print(f"图片已保存至: {save_path}")
    if show:
        plt.show()
    else:
        plt.close(fig)

# ----------------------------------------------------------------------
# 主程序（不变）
# ----------------------------------------------------------------------
def main():
    log_file = sys.argv[1] if len(sys.argv) > 1 else "points-log.txt"
    if not os.path.exists(log_file):
        print(f"错误: 文件 '{log_file}' 不存在")
        return

    with open(log_file, 'r', encoding='utf-8') as f:
        text = f.read()

    records = parse_meter_records(text)
    if not records:
        print("未找到任何仪表盘记录")
        return

    timestamp = time.strftime("%Y%m%d_%H%M%S")
    output_dir = os.environ.get("RUST_PORTAL_METER_OUTPUT") or f"meter_output_{timestamp}"
    os.makedirs(output_dir, exist_ok=True)
    print(f"共找到 {len(records)} 个仪表盘记录，输出目录：{output_dir}")

    for idx, rec in enumerate(records, start=1):
        print(f"\n--- 记录 {idx}/{len(records)} ---")
        print("  裁剪框:", rec.get('crop'))
        print("  最终读数:", rec.get('final_value'))
        save_path = os.path.join(output_dir, f"meter_{idx:03d}.png")
        plot_meter_record(rec,
                          title=f"仪表盘检测结果 (第 {idx} 条)",
                          save_path=save_path,
                          show=False)

    print(f"\n全部完成！所有图片保存在 '{output_dir}' 文件夹中。")

if __name__ == "__main__":
    main()
