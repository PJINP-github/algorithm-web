use crate::state::AppState;
use std::{
    collections::HashMap,
    path::{Path, PathBuf},
    sync::{Arc, Mutex, atomic::AtomicU64},
};

pub fn init(path: &Path, project_root: PathBuf) -> AppState {
    let conn = rusqlite::Connection::open(path).unwrap();
    conn.execute(
        "CREATE TABLE IF NOT EXISTS todos (id INTEGER PRIMARY KEY, title TEXT NOT NULL)",
        [],
    )
    .unwrap();
    conn.execute(
        "CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY,
            username TEXT NOT NULL UNIQUE,
            password TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT '普通一级'
        )",
        [],
    )
    .unwrap();
    let _ = conn.execute(
        "ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT '普通一级'",
        [],
    );
    conn.execute(
        "UPDATE users
         SET role = '管理员'
         WHERE lower(trim(username)) = 'admin'",
        [],
    )
    .unwrap();
    conn.execute(
        "CREATE TABLE IF NOT EXISTS authorization_codes (
            code TEXT PRIMARY KEY,
            role TEXT NOT NULL,
            used INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )",
        [],
    )
    .unwrap();
    conn.execute(
        "DELETE FROM authorization_codes
         WHERE rowid NOT IN (
             SELECT rowid FROM authorization_codes
             ORDER BY created_at DESC, rowid DESC
             LIMIT 5
         )",
        [],
    )
    .unwrap();

    if let Ok(code) = std::env::var("ADMIN_AUTH_CODE") {
        let code = code.trim().to_owned();
        if !code.is_empty() {
            let _ = conn.execute(
                "INSERT OR IGNORE INTO authorization_codes (code, role) VALUES (?1, '管理员')",
                [&code],
            );
        }
    } else {
        let admin_count = conn
            .query_row(
                "SELECT COUNT(*) FROM users WHERE role = '管理员'",
                [],
                |row| row.get::<_, i64>(0),
            )
            .unwrap_or(1);
        if admin_count == 0 {
            let _ = conn.execute(
                "INSERT OR IGNORE INTO authorization_codes (code, role) VALUES ('LOCAL-ADMIN-CHANGE-ME', '管理员')",
                [],
            );
        }
    }

    if let (Ok(username), Ok(password)) = (
        std::env::var("ADMIN_USERNAME"),
        std::env::var("ADMIN_PASSWORD"),
    ) {
        if !username.trim().is_empty() && !password.is_empty() {
            let _ = conn.execute(
                "INSERT OR IGNORE INTO users (username, password, role) VALUES (?1, ?2, '管理员')",
                rusqlite::params![username.trim(), password],
            );
        }
    }
    conn.execute(
        "DELETE FROM authorization_codes
         WHERE rowid NOT IN (
             SELECT rowid FROM authorization_codes
             ORDER BY created_at DESC, rowid DESC
             LIMIT 5
         )",
        [],
    )
    .unwrap();

    let authority = crate::authority::AuthorityConfig::load(&project_root);

    AppState {
        db: Arc::new(Mutex::new(conn)),
        sessions: Arc::new(Mutex::new(HashMap::new())),
        jobs: Arc::new(Mutex::new(HashMap::new())),
        job_controls: Arc::new(Mutex::new(HashMap::new())),
        job_semaphore: Arc::new(tokio::sync::Semaphore::new(4)),
        h_review_semaphore: Arc::new(tokio::sync::Semaphore::new(1)),
        authority: Arc::new(authority),
        sequence: Arc::new(AtomicU64::new(1)),
        project_root,
    }
}

pub async fn call<T: Send + 'static>(
    state: crate::state::AppState,
    f: impl FnOnce(&rusqlite::Connection) -> T + Send + 'static,
) -> T {
    tokio::task::spawn_blocking(move || f(&state.db.lock().unwrap()))
        .await
        .unwrap()
}
