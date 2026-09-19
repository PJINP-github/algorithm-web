use std::{
    collections::HashMap,
    path::PathBuf,
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, AtomicU64, Ordering},
    },
};

use rusqlite::Connection;

#[derive(Clone)]
pub struct AppState {
    pub db: Arc<Mutex<Connection>>,
    pub sessions: Arc<Mutex<HashMap<String, crate::models::SessionUser>>>,
    pub jobs: Arc<Mutex<HashMap<String, crate::models::JobInfo>>>,
    pub job_controls: Arc<Mutex<HashMap<String, JobControl>>>,
    pub job_semaphore: Arc<tokio::sync::Semaphore>,
    pub h_review_semaphore: Arc<tokio::sync::Semaphore>,
    pub authority: Arc<crate::authority::AuthorityConfig>,
    pub sequence: Arc<AtomicU64>,
    pub project_root: PathBuf,
}

#[derive(Clone, Default)]
pub struct JobControl {
    cancelled: Arc<AtomicBool>,
    process_id: Arc<Mutex<Option<u32>>>,
}

impl JobControl {
    pub fn cancel(&self) {
        self.cancelled.store(true, Ordering::SeqCst);
    }

    pub fn is_cancelled(&self) -> bool {
        self.cancelled.load(Ordering::SeqCst)
    }

    pub fn process_id(&self) -> Option<u32> {
        *self.process_id.lock().unwrap()
    }

    pub fn set_process_id(&self, process_id: Option<u32>) {
        *self.process_id.lock().unwrap() = process_id;
    }
}

impl AppState {
    pub fn next_id(&self, prefix: &str) -> String {
        let number = self.sequence.fetch_add(1, Ordering::Relaxed);
        format!("{prefix}-{}-{number}", unix_seconds())
    }
}

fn unix_seconds() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|value| value.as_secs())
        .unwrap_or_default()
}
