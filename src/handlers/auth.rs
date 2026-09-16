use axum::{
    Json as ResponseJson,
    extract::{Json, State},
    http::{HeaderMap, StatusCode},
    response::IntoResponse,
};

use crate::models::{AuthResponse, Credentials, SessionUser};

fn response(
    status: StatusCode,
    success: bool,
    message: &str,
    session: Option<(String, SessionUser)>,
) -> impl IntoResponse {
    let (token, username, role) = match session {
        Some((token, user)) => (Some(token), Some(user.username), Some(user.role)),
        None => (None, None, None),
    };
    (
        status,
        ResponseJson(AuthResponse {
            success,
            message: message.to_owned(),
            token,
            username,
            role,
        }),
    )
}

fn credentials(
    credentials: Credentials,
) -> Result<(String, String, String), (StatusCode, &'static str)> {
    let username = credentials.username.trim().to_owned();
    if username.is_empty() || credentials.password.is_empty() {
        return Err((StatusCode::BAD_REQUEST, "用户名和密码不能为空"));
    }
    Ok((
        username,
        credentials.password,
        credentials.authorization_code.trim().to_owned(),
    ))
}

pub fn session_from_headers(
    headers: &HeaderMap,
    state: &crate::state::AppState,
) -> Option<SessionUser> {
    let value = headers.get("authorization")?.to_str().ok()?;
    let token = value.strip_prefix("Bearer ")?;
    state.sessions.lock().ok()?.get(token).cloned()
}

pub fn require_session(
    headers: &HeaderMap,
    state: &crate::state::AppState,
) -> Result<SessionUser, (StatusCode, &'static str)> {
    session_from_headers(headers, state).ok_or((StatusCode::UNAUTHORIZED, "登录已失效，请重新登录"))
}

pub async fn register(
    State(state): State<crate::state::AppState>,
    Json(input): Json<Credentials>,
) -> impl IntoResponse {
    let (username, password, authorization_code) = match credentials(input) {
        Ok(value) => value,
        Err((status, message)) => return response(status, false, message, None),
    };

    if authorization_code.is_empty() {
        return response(StatusCode::FORBIDDEN, false, "注册需要授权码", None);
    }

    let result = crate::db::call(state, move |connection| {
        let role = connection
            .query_row(
                "SELECT role FROM authorization_codes WHERE code = ?1 AND used = 0",
                [&authorization_code],
                |row| row.get::<_, String>(0),
            )
            .map_err(|_| ())?;
        if username.eq_ignore_ascii_case("admin") && role != "管理员" {
            return Err(());
        }

        connection
            .execute(
                "INSERT INTO users (username, password, role) VALUES (?1, ?2, ?3)",
                rusqlite::params![username, password, role],
            )
            .map_err(|_| ())?;
        connection
            .execute(
                "UPDATE authorization_codes SET used = 1 WHERE code = ?1",
                [&authorization_code],
            )
            .map_err(|_| ())?;
        Ok::<(), ()>(())
    })
    .await;

    match result {
        Ok(()) => response(StatusCode::CREATED, true, "注册成功，请返回登录", None),
        Err(()) => response(
            StatusCode::FORBIDDEN,
            false,
            "授权码无效、已使用或用户名已存在",
            None,
        ),
    }
}

pub async fn login(
    State(state): State<crate::state::AppState>,
    Json(input): Json<Credentials>,
) -> impl IntoResponse {
    let (username, password, _) = match credentials(input) {
        Ok(value) => value,
        Err((status, message)) => return response(status, false, message, None),
    };

    let lookup_username = username.clone();
    let result = crate::db::call(state.clone(), move |connection| {
        match connection.query_row(
            "SELECT id, password, role FROM users WHERE username = ?1",
            [&lookup_username],
            |row| {
                Ok((
                    row.get::<_, i64>(0)?,
                    row.get::<_, String>(1)?,
                    row.get::<_, String>(2)?,
                ))
            },
        ) {
            Ok((id, stored_password, role)) => Ok(Some((id, stored_password == password, role))),
            Err(rusqlite::Error::QueryReturnedNoRows) => Ok(None),
            Err(_) => Err(()),
        }
    })
    .await;

    match result {
        Ok(Some((id, true, role))) => {
            let role = effective_role(&username, &role);
            let user = SessionUser { id, username, role };
            let token = state.next_id("session");
            state
                .sessions
                .lock()
                .unwrap()
                .insert(token.clone(), user.clone());
            response(StatusCode::OK, true, "登录成功", Some((token, user)))
        }
        Ok(Some((_, false, _))) | Ok(None) => {
            response(StatusCode::UNAUTHORIZED, false, "用户名或密码错误", None)
        }
        Err(()) => response(
            StatusCode::INTERNAL_SERVER_ERROR,
            false,
            "登录失败，请稍后重试",
            None,
        ),
    }
}

pub async fn logout(
    State(state): State<crate::state::AppState>,
    headers: HeaderMap,
) -> impl IntoResponse {
    if let Some(value) = headers
        .get("authorization")
        .and_then(|item| item.to_str().ok())
    {
        if let Some(token) = value.strip_prefix("Bearer ") {
            state.sessions.lock().unwrap().remove(token);
        }
    }
    response(StatusCode::OK, true, "已退出登录", None)
}

fn effective_role(username: &str, role: &str) -> String {
    if username.trim().eq_ignore_ascii_case("admin") {
        "管理员".to_owned()
    } else {
        role.to_owned()
    }
}
