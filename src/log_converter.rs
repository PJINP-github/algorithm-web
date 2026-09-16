use std::{
    collections::{HashMap, HashSet},
    fs,
    path::{Path, PathBuf},
};

use rust_xlsxwriter::Workbook;
use serde_json::{Map, Value};

const MAX_CELL_CHARS: usize = 30_000;

const REQUEST_HEADERS: [&str; 28] = [
    "序号",
    "日志文件",
    "行号",
    "识别时间戳",
    "日志记录时间",
    "分析响应耗时(ms)",
    "objectId",
    "requestId",
    "点位",
    "任务ID",
    "相机IP",
    "图片路径",
    "请求类型",
    "imageRecogType",
    "模型类别",
    "使用模型",
    "阶段序号",
    "分类类别",
    "中文结果描述",
    "结果类型",
    "返回码",
    "置信度",
    "分析区域",
    "结果位置",
    "回调目标",
    "回调状态",
    "异常级别",
    "备注信息",
];

const OVERVIEW_HEADERS: [&str; 10] = [
    "objectId",
    "结果类型",
    "中文结果描述",
    "置信度",
    "点位",
    "请求类型",
    "imageRecogType",
    "模型类别",
    "阶段序号",
    "使用模型",
];

const ANALYSE_HEADERS: [&str; 11] = [
    "序号",
    "日志文件",
    "行号",
    "objectId",
    "分析索引",
    "分析区域",
    "宽度",
    "高度",
    "置信度",
    "是否整图",
    "备注信息",
];

const STAGE_HEADERS: [&str; 8] = [
    "序号",
    "日志文件",
    "行号",
    "objectId",
    "模型类别",
    "使用模型",
    "阶段序号",
    "备注信息",
];

const ANOMALY_HEADERS: [&str; 10] = [
    "序号",
    "日志文件",
    "行号",
    "objectId",
    "requestId",
    "异常级别",
    "异常类型",
    "异常描述",
    "关联模型/类型",
    "原始内容",
];

const SUMMARY_HEADERS: [&str; 3] = ["指标", "值", "备注"];

#[derive(Default)]
struct ParsedLog {
    file_name: String,
    total_lines: usize,
    json_events: Vec<JsonEvent>,
    stages: Vec<StageEvent>,
    analyses: Vec<AnalyseEvent>,
    posts: Vec<PostEvent>,
    sync_codes: Vec<String>,
    anomalies: Vec<AnomalyEvent>,
    recognized_lines: usize,
}

struct JsonEvent {
    line_no: usize,
    log_time: String,
    log_time_ms: Option<i64>,
    payload: Value,
}

struct StageEvent {
    line_no: usize,
    object_id: String,
    kind: String,
    model: String,
    stage: String,
}

struct AnalyseEvent {
    line_no: usize,
    object_id: String,
    index: String,
    x1: i64,
    y1: i64,
    x2: i64,
    y2: i64,
    confidence: String,
}

struct PostEvent {
    host: String,
    port: String,
    request_id: String,
    body: String,
}

struct AnomalyEvent {
    line_no: usize,
    object_id: String,
    request_id: String,
    level: String,
    kind: String,
    message: String,
    related: String,
    raw: String,
}

struct WorkbookRows {
    request: Vec<Vec<String>>,
    overview: Vec<Vec<String>>,
    analyse: Vec<Vec<String>>,
    stage: Vec<Vec<String>>,
    anomaly: Vec<Vec<String>>,
    summary: Vec<Vec<String>>,
}

pub fn convert_txt_to_xlsx(input: &Path, output: &Path) -> Result<usize, String> {
    let text = fs::read_to_string(input).map_err(|error| format!("读取日志失败: {error}"))?;
    let parsed = parse_text(
        input
            .file_name()
            .and_then(|value| value.to_str())
            .unwrap_or("log.txt"),
        &text,
    );
    let rows = build_rows(&parsed);
    write_workbook(&rows, output)?;
    Ok(parsed.total_lines)
}

pub fn output_path_for(input: &Path, output_dir: &Path) -> PathBuf {
    let stem = input
        .file_stem()
        .and_then(|value| value.to_str())
        .unwrap_or("log");
    output_dir.join(format!("{stem}.xlsx"))
}

fn parse_text(file_name: &str, text: &str) -> ParsedLog {
    let mut parsed = ParsedLog {
        file_name: file_name.to_owned(),
        ..ParsedLog::default()
    };
    let mut current_object_id = String::new();

    for (index, raw_line) in text.lines().enumerate() {
        let line_no = index + 1;
        parsed.total_lines = line_no;
        let line = raw_line.trim_end_matches(['\r', '\n']);
        let mut recognized = false;

        if let Some((object_id, kind, model, stage)) = parse_stage(line) {
            current_object_id = object_id.clone();
            parsed.stages.push(StageEvent {
                line_no,
                object_id,
                kind,
                model,
                stage,
            });
            recognized = true;
        } else if let Some(values) = parse_analyse(line) {
            parsed.analyses.push(AnalyseEvent {
                line_no,
                object_id: current_object_id.clone(),
                index: values[0].clone(),
                x1: values[1].parse().unwrap_or_default(),
                y1: values[2].parse().unwrap_or_default(),
                x2: values[3].parse().unwrap_or_default(),
                y2: values[4].parse().unwrap_or_default(),
                confidence: values[5].clone(),
            });
            recognized = true;
        } else if let Some((timestamp, json_text)) = parse_debug_json(line) {
            match serde_json::from_str::<Value>(json_text) {
                Ok(payload) => {
                    let object_id = value_string(
                        payload
                            .get("Request")
                            .and_then(|value| value.get("objectId")),
                    );
                    if !object_id.is_empty() {
                        current_object_id = object_id;
                    }
                    parsed.json_events.push(JsonEvent {
                        line_no,
                        log_time_ms: parse_timestamp_ms(timestamp),
                        log_time: timestamp.to_owned(),
                        payload,
                    });
                }
                Err(error) => parsed.anomalies.push(AnomalyEvent {
                    line_no,
                    object_id: current_object_id.clone(),
                    request_id: String::new(),
                    level: "ERROR".to_owned(),
                    kind: "json_parse_error".to_owned(),
                    message: error.to_string(),
                    related: String::new(),
                    raw: line.to_owned(),
                }),
            }
            recognized = true;
        } else if let Some(post) = parse_post(line) {
            parsed.posts.push(post);
            recognized = true;
        } else if let Some(code) = line.strip_prefix("sync-return ") {
            parsed.sync_codes.push(code.trim().to_owned());
            recognized = true;
        } else if let Some(message) = line.strip_prefix("[WARN] ") {
            parsed.anomalies.push(AnomalyEvent {
                line_no,
                object_id: current_object_id.clone(),
                request_id: String::new(),
                level: "WARN".to_owned(),
                kind: "warning".to_owned(),
                message: message.to_owned(),
                related: String::new(),
                raw: line.to_owned(),
            });
            recognized = true;
        } else if let Some(body) = line.strip_prefix("[T3E] Error @ ") {
            let request_id = quoted_value(body, "requestId");
            let message = quoted_value(body, "desc");
            parsed.anomalies.push(AnomalyEvent {
                line_no,
                object_id: current_object_id.clone(),
                request_id,
                level: "ERROR".to_owned(),
                kind: "algorithm_error".to_owned(),
                message: if message.is_empty() {
                    body.to_owned()
                } else {
                    message
                },
                related: String::new(),
                raw: line.to_owned(),
            });
            recognized = true;
        } else if line.contains("ERROR") {
            parsed.anomalies.push(AnomalyEvent {
                line_no,
                object_id: current_object_id.clone(),
                request_id: String::new(),
                level: "ERROR".to_owned(),
                kind: "unclassified_error".to_owned(),
                message: line.to_owned(),
                related: String::new(),
                raw: line.to_owned(),
            });
            recognized = true;
        }

        if recognized {
            parsed.recognized_lines += 1;
        }
    }
    parsed
}

fn build_rows(parsed: &ParsedLog) -> WorkbookRows {
    let mut stages_by_object: HashMap<String, Vec<&StageEvent>> = HashMap::new();
    let mut analyses_by_object: HashMap<String, Vec<&AnalyseEvent>> = HashMap::new();
    let mut posts_by_request: HashMap<String, &PostEvent> = HashMap::new();

    for stage in &parsed.stages {
        stages_by_object
            .entry(stage.object_id.clone())
            .or_default()
            .push(stage);
    }
    for analyse in &parsed.analyses {
        analyses_by_object
            .entry(analyse.object_id.clone())
            .or_default()
            .push(analyse);
    }
    for post in &parsed.posts {
        posts_by_request.insert(post.request_id.clone(), post);
    }

    let mut request_rows = Vec::new();
    let mut overview_rows = Vec::new();
    let mut anomaly_rows = Vec::new();

    for event in &parsed.json_events {
        let request = event
            .payload
            .get("Request")
            .and_then(Value::as_object)
            .cloned()
            .unwrap_or_default();
        let response = event
            .payload
            .get("Response")
            .and_then(Value::as_object)
            .cloned()
            .unwrap_or_default();
        let object_id = first_nonempty(
            value_string(request.get("objectId")),
            value_string(response.get("objectId")),
        );
        let type_list = array_strings(request.get("typeList"));
        let image_recog_type = value_string(request.get("imageRecogType"));
        let image_path = array_strings(request.get("imageUrlList"))
            .into_iter()
            .next()
            .unwrap_or_default();
        let (task_id, camera_ip, request_id, point) = image_info(&image_path);
        let request_time = value_string(request.get("timestamp"));
        let elapsed_ms = match (parse_timestamp_ms(&request_time), event.log_time_ms) {
            (Some(start), Some(end)) => (end - start).to_string(),
            _ => String::new(),
        };

        let stages = stages_by_object
            .get(&object_id)
            .cloned()
            .unwrap_or_default();
        let stage_kinds = unique_join(
            stages
                .iter()
                .map(|stage| stage.kind.as_str())
                .chain(type_list.iter().map(String::as_str))
                .chain(std::iter::once(image_recog_type.as_str())),
        );
        let models = unique_join(stages.iter().map(|stage| stage.model.as_str()));
        let stage_numbers = unique_join(stages.iter().map(|stage| stage.stage.as_str()));

        let analyses = analyses_by_object
            .get(&object_id)
            .cloned()
            .unwrap_or_default();
        let analyse_text = analyses
            .iter()
            .map(|item| analyse_area(item))
            .collect::<Vec<_>>()
            .join(" | ");
        let post = posts_by_request.get(&request_id).copied();
        let callback_target = post
            .map(|value| format!("{}:{}", value.host, value.port))
            .unwrap_or_default();
        let callback_status = post.map(|value| value.body.clone()).unwrap_or_default();

        let results = response
            .get("results")
            .and_then(Value::as_array)
            .filter(|items| !items.is_empty())
            .cloned()
            .unwrap_or_else(|| {
                anomaly_rows.push(anomaly_row(
                    anomaly_rows.len() + 1,
                    &parsed.file_name,
                    event.line_no,
                    &object_id,
                    &request_id,
                    "WARN",
                    "empty_results",
                    "Response.results 为空",
                    &image_recog_type,
                    &json_compact(&Value::Object(response.clone())),
                ));
                vec![Value::Object(Map::new())]
            });

        for result in results {
            let result = result.as_object().cloned().unwrap_or_default();
            let code = value_string(result.get("code"));
            let description = value_string(result.get("desc"));
            let category = value_string(result.get("value"));
            let confidence = value_string(result.get("conf"));
            let result_type = value_string(result.get("type"));
            let result_position = result.get("pos").map(json_compact).unwrap_or_default();
            let mut notes = Vec::new();
            let mut anomaly_level = String::new();

            if !code.is_empty() && code != "2000" {
                anomaly_level = "WARN".to_owned();
                notes.push(format!("返回码非2000: {code}"));
                anomaly_rows.push(anomaly_row(
                    anomaly_rows.len() + 1,
                    &parsed.file_name,
                    event.line_no,
                    &object_id,
                    &request_id,
                    "WARN",
                    "non_2000_code",
                    &format!("返回码 {code}，描述：{description}"),
                    if result_type.is_empty() {
                        &image_recog_type
                    } else {
                        &result_type
                    },
                    &json_compact(&Value::Object(result.clone())),
                ));
            }
            if description.to_ascii_uppercase().contains("ERROR") {
                anomaly_level = "ERROR".to_owned();
                notes.push("结果描述包含 ERROR".to_owned());
            }

            request_rows.push(vec![
                (request_rows.len() + 1).to_string(),
                parsed.file_name.clone(),
                event.line_no.to_string(),
                request_time.clone(),
                event.log_time.clone(),
                elapsed_ms.clone(),
                object_id.clone(),
                request_id.clone(),
                point.clone(),
                task_id.clone(),
                camera_ip.clone(),
                image_path.clone(),
                type_list.join(", "),
                image_recog_type.clone(),
                stage_kinds.clone(),
                models.clone(),
                stage_numbers.clone(),
                category,
                description.clone(),
                result_type.clone(),
                code,
                confidence.clone(),
                analyse_text.clone(),
                result_position,
                callback_target.clone(),
                callback_status.clone(),
                anomaly_level,
                notes.join("；"),
            ]);

            let unique_stages = unique_stage_rows(&stages);
            if unique_stages.is_empty() {
                overview_rows.push(vec![
                    object_id.clone(),
                    result_type.clone(),
                    description.clone(),
                    confidence.clone(),
                    point.clone(),
                    type_list.join(", "),
                    image_recog_type.clone(),
                    image_recog_type.clone(),
                    String::new(),
                    String::new(),
                ]);
            } else {
                for stage in unique_stages {
                    overview_rows.push(vec![
                        object_id.clone(),
                        result_type.clone(),
                        description.clone(),
                        confidence.clone(),
                        point.clone(),
                        type_list.join(", "),
                        image_recog_type.clone(),
                        stage.kind,
                        stage.stage,
                        stage.model,
                    ]);
                }
            }
        }
    }

    let analyse_rows = parsed
        .analyses
        .iter()
        .enumerate()
        .map(|(index, item)| {
            let width = item.x2 - item.x1;
            let height = item.y2 - item.y1;
            vec![
                (index + 1).to_string(),
                parsed.file_name.clone(),
                item.line_no.to_string(),
                item.object_id.clone(),
                item.index.clone(),
                analyse_area(item),
                width.to_string(),
                height.to_string(),
                item.confidence.clone(),
                if is_full_image(item) { "是" } else { "否" }.to_owned(),
                if width <= 0 || height <= 0 {
                    "区域宽高异常".to_owned()
                } else {
                    String::new()
                },
            ]
        })
        .collect();

    let stage_rows = parsed
        .stages
        .iter()
        .enumerate()
        .map(|(index, item)| {
            vec![
                (index + 1).to_string(),
                parsed.file_name.clone(),
                item.line_no.to_string(),
                item.object_id.clone(),
                item.kind.clone(),
                item.model.clone(),
                item.stage.clone(),
                String::new(),
            ]
        })
        .collect();

    for item in &parsed.anomalies {
        anomaly_rows.push(anomaly_row(
            anomaly_rows.len() + 1,
            &parsed.file_name,
            item.line_no,
            &item.object_id,
            &item.request_id,
            &item.level,
            &item.kind,
            &item.message,
            &item.related,
            &item.raw,
        ));
    }

    WorkbookRows {
        request: request_rows.clone(),
        overview: overview_rows,
        analyse: analyse_rows,
        stage: stage_rows,
        anomaly: anomaly_rows.clone(),
        summary: summary_rows(parsed, &request_rows, &anomaly_rows),
    }
}

fn summary_rows(
    parsed: &ParsedLog,
    request_rows: &[Vec<String>],
    anomaly_rows: &[Vec<String>],
) -> Vec<Vec<String>> {
    let mut request_types: HashMap<String, usize> = HashMap::new();
    let mut result_codes: HashMap<String, usize> = HashMap::new();
    let mut anomaly_levels: HashMap<String, usize> = HashMap::new();
    for row in request_rows {
        let key = if row.get(13).map(String::is_empty).unwrap_or(true) {
            row.get(12).cloned().unwrap_or_default()
        } else {
            row.get(13).cloned().unwrap_or_default()
        };
        *request_types.entry(key).or_default() += 1;
        *result_codes
            .entry(row.get(20).cloned().unwrap_or_default())
            .or_default() += 1;
    }
    for row in anomaly_rows {
        *anomaly_levels
            .entry(row.get(5).cloned().unwrap_or_default())
            .or_default() += 1;
    }

    let callback_success = parsed
        .posts
        .iter()
        .filter(|post| post.body.replace(' ', "") == r#"{"code":200}"#)
        .count();
    let mut rows = vec![
        vec!["源文件".to_owned(), parsed.file_name.clone(), String::new()],
        vec![
            "总行数".to_owned(),
            parsed.total_lines.to_string(),
            String::new(),
        ],
        vec![
            "识别到的结构化行数".to_owned(),
            parsed.recognized_lines.to_string(),
            "T3I / DEBUG JSON / analyse_ret / POST_RET / WARN / ERROR / sync-return".to_owned(),
        ],
        vec![
            "请求结果行数".to_owned(),
            request_rows.len().to_string(),
            "一条 Response.results 会展开为一行".to_owned(),
        ],
        vec![
            "DEBUG JSON 数量".to_owned(),
            parsed.json_events.len().to_string(),
            String::new(),
        ],
        vec![
            "算法阶段 T3I 数量".to_owned(),
            parsed.stages.len().to_string(),
            String::new(),
        ],
        vec![
            "analyse_ret 数量".to_owned(),
            parsed.analyses.len().to_string(),
            String::new(),
        ],
        vec![
            "POST_RET 数量".to_owned(),
            parsed.posts.len().to_string(),
            String::new(),
        ],
        vec![
            "sync-return 数量".to_owned(),
            parsed.sync_codes.len().to_string(),
            String::new(),
        ],
        vec![
            "POST_RET code=200 数量".to_owned(),
            callback_success.to_string(),
            String::new(),
        ],
        vec![
            "异常/警告行数".to_owned(),
            anomaly_rows.len().to_string(),
            String::new(),
        ],
    ];
    append_counter_rows(&mut rows, "请求类型：", request_types);
    append_counter_rows(&mut rows, "返回码：", result_codes);
    append_counter_rows(&mut rows, "异常级别：", anomaly_levels);
    rows
}

fn append_counter_rows(rows: &mut Vec<Vec<String>>, prefix: &str, values: HashMap<String, usize>) {
    let mut values = values.into_iter().collect::<Vec<_>>();
    values.sort_by(|left, right| left.0.cmp(&right.0));
    for (key, count) in values {
        rows.push(vec![
            format!("{prefix}{key}"),
            count.to_string(),
            String::new(),
        ]);
    }
}

fn write_workbook(rows: &WorkbookRows, output: &Path) -> Result<(), String> {
    let mut workbook = Workbook::new();
    write_sheet(&mut workbook, "总览", &OVERVIEW_HEADERS, &rows.overview)?;
    write_sheet(&mut workbook, "请求汇总", &REQUEST_HEADERS, &rows.request)?;
    write_sheet(&mut workbook, "分析明细", &ANALYSE_HEADERS, &rows.analyse)?;
    write_sheet(&mut workbook, "阶段模型", &STAGE_HEADERS, &rows.stage)?;
    write_sheet(&mut workbook, "异常与警告", &ANOMALY_HEADERS, &rows.anomaly)?;
    write_sheet(&mut workbook, "文件统计", &SUMMARY_HEADERS, &rows.summary)?;

    if let Some(parent) = output.parent() {
        fs::create_dir_all(parent).map_err(|error| format!("创建输出目录失败: {error}"))?;
    }
    workbook
        .save(output)
        .map_err(|error| format!("保存 XLSX 失败: {error}"))?;
    Ok(())
}

fn write_sheet(
    workbook: &mut Workbook,
    name: &str,
    headers: &[&str],
    rows: &[Vec<String>],
) -> Result<(), String> {
    let sheet = workbook.add_worksheet();
    sheet
        .set_name(name)
        .map_err(|error| format!("创建工作表 {name} 失败: {error}"))?;
    for (column, header) in headers.iter().enumerate() {
        sheet
            .write_string(0, column as u16, *header)
            .map_err(|error| format!("写入 {name} 表头失败: {error}"))?;
    }
    for (row_index, row) in rows.iter().enumerate() {
        for (column, value) in row.iter().enumerate() {
            sheet
                .write_string((row_index + 1) as u32, column as u16, truncate(value))
                .map_err(|error| format!("写入 {name} 数据失败: {error}"))?;
        }
    }
    sheet
        .set_freeze_panes(1, 0)
        .map_err(|error| format!("冻结 {name} 表头失败: {error}"))?;
    for column in 0..headers.len() {
        sheet
            .set_column_width(column as u16, if column == 0 { 12.0 } else { 24.0 })
            .map_err(|error| format!("设置 {name} 列宽失败: {error}"))?;
    }
    Ok(())
}

fn parse_stage(line: &str) -> Option<(String, String, String, String)> {
    let rest = line.strip_prefix("[T3I] ")?;
    let (object_id, remainder) = rest.split_once(" // ")?;
    let (kind, remainder) = remainder.split_once('@')?;
    let (model, stage) = remainder.split_once(" : ")?;
    Some((
        object_id.trim().to_owned(),
        kind.trim().to_owned(),
        model.trim().to_owned(),
        stage.trim().to_owned(),
    ))
}

fn parse_analyse(line: &str) -> Option<Vec<String>> {
    let rest = line.strip_prefix("analyse_ret @ ")?;
    let (index, coordinates) = rest.split_once(" : ")?;
    let numbers = signed_numbers(coordinates);
    if numbers.len() < 4 {
        return None;
    }
    let confidence = coordinates.split_whitespace().last().unwrap_or_default();
    Some(vec![
        index.trim().to_owned(),
        numbers[0].to_string(),
        numbers[1].to_string(),
        numbers[2].to_string(),
        numbers[3].to_string(),
        confidence.to_owned(),
    ])
}

fn parse_debug_json(line: &str) -> Option<(&str, &str)> {
    let (timestamp, json) = line.split_once(" - trueno3 - DEBUG - ")?;
    if parse_timestamp_ms(timestamp).is_some() && json.trim_start().starts_with('{') {
        Some((timestamp, json.trim()))
    } else {
        None
    }
}

fn parse_post(line: &str) -> Option<PostEvent> {
    let rest = line.strip_prefix("[POST_RET] ")?;
    let (prefix, body) = rest.split_once(" @ ")?;
    let mut parts = prefix.split_whitespace();
    let host = parts.next()?.to_owned();
    let port = parts.next()?.to_owned();
    let request_id = parts
        .next()?
        .trim_start_matches('[')
        .trim_end_matches(']')
        .to_owned();
    Some(PostEvent {
        host,
        port,
        request_id,
        body: body.to_owned(),
    })
}

fn signed_numbers(value: &str) -> Vec<i64> {
    let mut numbers = Vec::new();
    let mut current = String::new();
    for character in value.chars() {
        if character.is_ascii_digit() || (character == '-' && current.is_empty()) {
            current.push(character);
        } else if !current.is_empty() {
            if let Ok(number) = current.parse() {
                numbers.push(number);
            }
            current.clear();
        }
    }
    if let Ok(number) = current.parse() {
        numbers.push(number);
    }
    numbers
}

fn parse_timestamp_ms(value: &str) -> Option<i64> {
    let (date, time) = value.trim().split_once(' ')?;
    let date_parts = date
        .split('-')
        .map(|part| part.parse::<i64>().ok())
        .collect::<Option<Vec<_>>>()?;
    if date_parts.len() != 3 {
        return None;
    }
    let mut time_parts = time.split(':');
    let hour = time_parts.next()?.parse::<i64>().ok()?;
    let minute = time_parts.next()?.parse::<i64>().ok()?;
    let seconds = time_parts.next()?;
    let (second, millis) = seconds
        .split_once(',')
        .map(|(second, millis)| (second, millis))
        .unwrap_or((seconds, "0"));
    let second = second.parse::<i64>().ok()?;
    let millis = millis.parse::<i64>().ok()?;
    let days = days_from_civil(date_parts[0], date_parts[1], date_parts[2]);
    Some((((days * 24 + hour) * 60 + minute) * 60 + second) * 1000 + millis)
}

fn days_from_civil(year: i64, month: i64, day: i64) -> i64 {
    let year = year - i64::from(month <= 2);
    let era = if year >= 0 { year } else { year - 399 } / 400;
    let year_of_era = year - era * 400;
    let month_adjusted = month + if month > 2 { -3 } else { 9 };
    let day_of_year = (153 * month_adjusted + 2) / 5 + day - 1;
    let day_of_era = year_of_era * 365 + year_of_era / 4 - year_of_era / 100 + day_of_year;
    era * 146097 + day_of_era - 719468
}

fn image_info(image_path: &str) -> (String, String, String, String) {
    let task_id = image_path
        .split("/image/task/")
        .nth(1)
        .and_then(|value| value.split('/').next())
        .unwrap_or_default()
        .to_owned();
    let camera_ip = image_path
        .find("192.168.3.")
        .map(|start| {
            image_path[start..]
                .split('_')
                .next()
                .unwrap_or_default()
                .to_owned()
        })
        .unwrap_or_default();
    let request_id = image_path
        .split("_1_")
        .nth(1)
        .and_then(|value| value.split(".jpg").next())
        .unwrap_or_default()
        .to_owned();
    let point = match (task_id.is_empty(), camera_ip.is_empty()) {
        (false, false) => format!("task/{task_id} @ {camera_ip}"),
        (false, true) => format!("task/{task_id}"),
        (true, false) => camera_ip.clone(),
        (true, true) => String::new(),
    };
    (task_id, camera_ip, request_id, point)
}

fn unique_join<'a>(values: impl Iterator<Item = &'a str>) -> String {
    let mut seen = HashSet::new();
    values
        .filter(|value| !value.is_empty())
        .filter(|value| seen.insert((*value).to_owned()))
        .collect::<Vec<_>>()
        .join(" | ")
}

fn unique_stage_rows(stages: &[&StageEvent]) -> Vec<StageEvent> {
    let mut seen = HashSet::new();
    stages
        .iter()
        .filter(|stage| seen.insert((stage.kind.clone(), stage.model.clone(), stage.stage.clone())))
        .map(|stage| StageEvent {
            line_no: stage.line_no,
            object_id: stage.object_id.clone(),
            kind: stage.kind.clone(),
            model: stage.model.clone(),
            stage: stage.stage.clone(),
        })
        .collect()
}

fn analyse_area(item: &AnalyseEvent) -> String {
    format!("(({}, {}), ({}, {}))", item.x1, item.y1, item.x2, item.y2)
}

fn is_full_image(item: &AnalyseEvent) -> bool {
    matches!(
        (item.x1, item.y1, item.x2, item.y2),
        (0, 0, 1920, 1080) | (0, 0, 2560, 1440)
    )
}

fn anomaly_row(
    index: usize,
    file_name: &str,
    line_no: usize,
    object_id: &str,
    request_id: &str,
    level: &str,
    kind: &str,
    message: &str,
    related: &str,
    raw: &str,
) -> Vec<String> {
    vec![
        index.to_string(),
        file_name.to_owned(),
        line_no.to_string(),
        object_id.to_owned(),
        request_id.to_owned(),
        level.to_owned(),
        kind.to_owned(),
        message.to_owned(),
        related.to_owned(),
        truncate(raw),
    ]
}

fn array_strings(value: Option<&Value>) -> Vec<String> {
    value
        .and_then(Value::as_array)
        .map(|items| items.iter().map(|item| value_string(Some(item))).collect())
        .unwrap_or_default()
}

fn value_string(value: Option<&Value>) -> String {
    match value {
        Some(Value::String(value)) => value.clone(),
        Some(Value::Null) | None => String::new(),
        Some(value) => value.to_string(),
    }
}

fn first_nonempty(first: String, second: String) -> String {
    if first.is_empty() { second } else { first }
}

fn quoted_value(value: &str, key: &str) -> String {
    for (marker, terminator) in [
        (format!("'{key}': '"), '\''),
        (format!("\"{key}\": \""), '"'),
    ] {
        if let Some(start) = value.find(&marker) {
            let rest = &value[start + marker.len()..];
            if let Some(end) = rest.find(terminator) {
                return rest[..end].to_owned();
            }
        }
    }
    String::new()
}

fn json_compact(value: &Value) -> String {
    serde_json::to_string(value).unwrap_or_else(|_| value.to_string())
}

fn truncate(value: &str) -> String {
    let mut chars = value.chars();
    let result = chars.by_ref().take(MAX_CELL_CHARS).collect::<String>();
    if chars.next().is_some() {
        format!("{result}...[已截断]")
    } else {
        result
    }
}
