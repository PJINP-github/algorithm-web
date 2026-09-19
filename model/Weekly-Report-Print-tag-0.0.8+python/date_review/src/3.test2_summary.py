#!/usr/bin/env python3
"""
Parse Tree.txt tree structure and summarize 发电/电网 sections from line 136 onwards.

Rules:
1. Section header row (td[0]="发电"/"电网"): 
   - Extract project name from td[1], station from td[3]
   - Extract first data entry from td[8] (algo) and td[9] (date)
2. Data rows (td[0] is empty/svg/"-"):
   - Extract algo from td[1], date from td[2]
3. Station change rows (td[0] has station name like "35kV大麻扎站"):
   - Update current station, skip row (no algo data)
4. Missing entries: print "@：该结构没被总结"
5. Empty [ ] should not be printed
"""

import re
import sys
import os

def parse_tree(filepath, start_line=136):
    """Parse the tree structure and return structured blocks."""
    with open(filepath, 'r', encoding='utf-8') as f:
        lines = f.readlines()
    
    lines = lines[start_line - 1:]
    raw_lines = [l.rstrip('\n') for l in lines if l.strip()]
    
    result_blocks = []
    current_block = None
    unmatched_entries = []
    
    row_cells = []
    current_cell_texts = []
    
    for line_idx, line in enumerate(raw_lines):
        # Find connector position
        connector_pos = -1
        for connector in ['├──', '└──']:
            pos = line.find(connector)
            if pos != -1:
                connector_pos = pos
                break
        
        if connector_pos == -1:
            continue
        
        depth = connector_pos // 4
        
        tag_part = line[connector_pos + 4:].strip()
        tag_match = re.match(r'^(\w+)', tag_part)
        tag = tag_match.group(1) if tag_match else None
        
        if depth == 0 and tag == 'tr':
            # New row - process previous row
            # Always save current cell, even if empty
            row_cells.append(current_cell_texts)
            current_cell_texts = []
            
            if row_cells:
                current_block = process_row(row_cells, result_blocks, current_block, unmatched_entries)
            row_cells = []
        
        elif depth == 1 and tag == 'td':
            # New cell - always save previous cell (even empty)
            row_cells.append(current_cell_texts)
            current_cell_texts = []
        
        # Extract quoted text
        m = re.search(r'"([^"]*)"', line)
        if m:
            current_cell_texts.append(m.group(1))
    
    # Process last row
    row_cells.append(current_cell_texts)
    if row_cells:
        process_row(row_cells, result_blocks, current_block, unmatched_entries)
    
    return result_blocks, unmatched_entries


def is_station_name(text):
    """Check if text looks like a station name."""
    if not text:
        return False
    return ('kV' in text or '站' in text or '变电站' in text or '变电所' in text 
            or '电厂' in text or '风电场' in text or '风电' in text or '光伏' in text)


def process_row(cells, result_blocks, current_block, unmatched_entries):
    """Process a parsed row."""
    # Filter out 'svg' from cells but keep cells
    cell_texts = []
    for cell in cells:
        filtered = [t for t in cell if t != 'svg']
        cell_texts.append(filtered)
    
    if not cell_texts:
        return current_block
    
    # Strip leading empty cells (caused by td boundary detection)
    # Also strip cells with only "-" or "无数据" (placeholders)
    stripped_count = 0
    while cell_texts and (not cell_texts[0] or cell_texts[0] == ['-'] or cell_texts[0] == ['无数据']):
        cell_texts.pop(0)
        stripped_count += 1
    
    # original_first_is_placeholder: True if MORE than just the td-boundary
    # empty cell was stripped (i.e. the row has leading "-" or "无数据" cells)
    # Station change rows have only the td-boundary empty cell stripped (count=1)
    # Data rows have the td-boundary + "-" or "无数据" stripped (count>=2)
    original_first_is_placeholder = (stripped_count > 1)
    
    if not cell_texts:
        return current_block
    
    # Find first non-empty cell and its text
    first_text = cell_texts[0][0] if cell_texts[0] else ''
    
    if first_text in ('发电', '电网'):
        # === Section Header Row ===
        business = first_text
        
        # Project name from td[1]
        project_name = cell_texts[1][0] if len(cell_texts) > 1 and cell_texts[1] else ''
        
        # Station name from td[3]
        station_name = ''
        if len(cell_texts) > 3 and cell_texts[3]:
            station_name = cell_texts[3][0]
        
        current_block = {
            'business': business,
            'project_name': project_name,
            'stations': [station_name],
            'station_entries': {station_name: []},
            'current_station': station_name
        }
        result_blocks.append(current_block)
        
        # Extract first data entry from section header (td[8] and td[9])
        if len(cell_texts) > 8 and cell_texts[8]:
            algo_type = cell_texts[8][0] if len(cell_texts[8]) > 0 else ''
            feature = cell_texts[8][1] if len(cell_texts[8]) > 1 else ''
            date = cell_texts[9][0] if len(cell_texts) > 9 and cell_texts[9] else ''
            
            if algo_type:
                current_block['station_entries'][station_name].append({
                    'algo_type': algo_type,
                    'feature': feature,
                    'date': date
                })
    
    elif current_block is not None:
        # === Data Row or Station Change Row ===
        
        # Check if this is a station change row
        # Only check if original first cell was NOT a placeholder
        is_station_change = False
        if not original_first_is_placeholder:
            if len(cell_texts) > 0 and len(cell_texts[0]) >= 2:
                # Check if second text is a date (YYYY-MM-DD or YYYY-M-D)
                second_text = cell_texts[0][1]
                if re.match(r'^\d{4}-\d{1,2}-\d{1,2}$', second_text):
                    is_station_change = True
            if not is_station_change and is_station_name(first_text):
                is_station_change = True
            if not is_station_change and first_text and first_text not in ('未知状态', '无数据', 'svg'):
                # Fallback: check if remaining cells are all status-like (station-only row)
                all_status = True
                for cell in cell_texts[1:]:
                    cell_text = cell[0] if cell else ''
                    if cell_text == '' or cell_text == '-' or cell_text == '无数据' or cell_text == '未知状态':
                        continue
                    if re.match(r'^\d+(\.\d+)?%$', cell_text):
                        continue
                    all_status = False
                    break
                if all_status:
                    is_station_change = True
        
        if is_station_change:
            # Station change row - update station
            new_station = first_text
            if new_station not in current_block['stations']:
                current_block['stations'].append(new_station)
                current_block['station_entries'][new_station] = []
            current_block['current_station'] = new_station
            # Don't return! Check if this row also has algo data in later cells
        
        # Data row processing
        # After stripping, cell[0] has algo, cell[1] has date
        # But if this is a station change row, cell[0] has station+date
        # so algo is in cell[1] and date in cell[2]
        if is_station_change:
            # Station change row: algo in cell[1], date in cell[2]
            algo_cell = cell_texts[1] if len(cell_texts) > 1 else []
            date_cell = cell_texts[2] if len(cell_texts) > 2 else []
        else:
            # Normal data row: algo in cell[0], date in cell[1]
            algo_cell = cell_texts[0] if len(cell_texts) > 0 else []
            date_cell = cell_texts[1] if len(cell_texts) > 1 else []
        
        algo_type = algo_cell[0] if len(algo_cell) > 0 else ''
        feature = algo_cell[1] if len(algo_cell) > 1 else ''
        date = date_cell[0] if date_cell else ''
        
        # Check if this looks like a valid data entry
        # Date should be like "11-04", "3-20", etc. (not "无数据", "100%", etc.)
        is_valid_date = bool(re.match(r'^\d{1,2}-\d{1,2}$', date)) if date else False
        
        if algo_type and (is_valid_date or date == '无数据'):
            current_block['station_entries'][current_block['current_station']].append({
                'algo_type': algo_type,
                'feature': feature,
                'date': date
            })
        elif algo_type:
            # Has algo_type but date doesn't match expected format
            # This might be an unmatched entry
            unmatched_entries.append({
                'project': current_block['project_name'],
                'station': current_block['current_station'],
                'algo_type': algo_type,
                'feature': feature,
                'date': date,
                'reason': '日期格式异常'
            })
    
    return current_block


def format_summary(blocks, unmatched_entries):
    """Format the summary output."""
    result = []
    
    for block_idx, block in enumerate(blocks):
        if block_idx > 0:
            result.append('')
            result.append('')
        
        project_name = block['project_name']
        stations = block['stations']
        station_entries = block['station_entries']
        
        # Print project name once
        result.append(project_name)
        
        for station in stations:
            entries = station_entries.get(station, [])
            result.append(f'---{station}')
            
            for entry in entries:
                date = entry['date']
                algo_type = entry['algo_type']
                feature = entry['feature']
                
                if feature:
                    result.append(f'-----  {date}  {algo_type}  [ {feature} ]')
                else:
                    result.append(f'-----  {date}  {algo_type}')
    
    # Add unmatched entries
    if unmatched_entries:
        result.append('')
        result.append('')
        result.append('=' * 60)
        result.append('以下条目未被正常总结：')
        result.append('=' * 60)
        for ue in unmatched_entries:
            result.append(f'{ue["date"]}  {ue["algo_type"]}  @：该结构没被总结  (项目: {ue["project"]}, 站点: {ue["station"]})')
    
    return '\n'.join(result)


def main():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    base_dir = os.path.dirname(script_dir)
    work_dir = os.path.join(base_dir, '0Work')
    os.makedirs(work_dir, exist_ok=True)
    
    filepath = os.path.join(work_dir, 'Tree.txt')
    
    blocks, unmatched_entries = parse_tree(filepath, start_line=136)
    
    output = format_summary(blocks, unmatched_entries)
    
    output_file = os.path.join(work_dir, 'z_树结构数据.txt')
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write(output)
    
    print(f'Summary written to {output_file}')
    print(f'Total blocks: {len(blocks)}')
    for b in blocks:
        total_entries = sum(len(v) for v in b['station_entries'].values())
        print(f'  {b["business"]}: {b["project_name"]}  ({len(b["stations"])} stations, {total_entries} entries)')
    
    print(f'\nUnmatched entries: {len(unmatched_entries)}')
    for ue in unmatched_entries:
        print(f'  {ue["date"]}  {ue["algo_type"]}  @：该结构没被总结  ({ue["project"]}, {ue["station"]})')


if __name__ == '__main__':
    main()