mod authority;
mod state;
// 声明 state 模块，存放 AppState 定义
mod db;
// 声明 db 模块，存放数据库初始化和通用查询包装
mod models;
// 声明 models 模块，存放数据模型
mod views;
// 声明 views 模块，存放 Maud 页面模板
mod handlers;
// 声明 handlers 模块，存放请求处理函数
mod log_converter;
mod routes;
// 声明 routes 模块，存放路由组装逻辑
#[tokio::main]
// Tokio 异步主函数，自动初始化多线程运行时
async fn main() {
    let project_root = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    let db_path = std::env::var("DB_PATH")
        .ok()
        .map(|value| anchored_path(&project_root, &value))
        .unwrap_or_else(|| project_root.join("todo.db"));
    let state = db::init(&db_path);
    let app = routes::build(state);
    let requested_port = std::env::var("PORT")
        .ok()
        .and_then(|value| value.parse::<u16>().ok())
        .unwrap_or(3000);
    let listener = bind_listener(requested_port).await;
    let address = listener.local_addr().expect("读取监听地址失败");
    let port = address.port();
    let url = format!("http://127.0.0.1:{port}");
    println!("工作区已启动：{url}");
    if let Some(lan_url) = lan_url(port) {
        println!("局域网访问：{lan_url}");
    }
    if std::env::var("OPEN_BROWSER").as_deref() != Ok("0") {
        let browser_url = url.clone();
        tokio::task::spawn_blocking(move || {
            let _ = open_with_default_app(&browser_url);
        });
    }
    axum::serve(
        listener,
        app.into_make_service_with_connect_info::<std::net::SocketAddr>(),
    )
    .await
    .unwrap();
}

fn anchored_path(root: &std::path::Path, value: &str) -> std::path::PathBuf {
    let path = std::path::Path::new(value.trim());
    if path.is_absolute() {
        path.to_path_buf()
    } else {
        root.join(path)
    }
}

async fn bind_listener(requested_port: u16) -> tokio::net::TcpListener {
    for offset in 0..20 {
        let port = requested_port.saturating_add(offset);
        if let Ok(listener) = tokio::net::TcpListener::bind(("0.0.0.0", port)).await {
            return listener;
        }
    }
    panic!(
        "无法绑定端口 {} 至 {}",
        requested_port,
        requested_port.saturating_add(19)
    );
}

fn lan_url(port: u16) -> Option<String> {
    let socket = std::net::UdpSocket::bind("0.0.0.0:0").ok()?;
    socket.connect("8.8.8.8:80").ok()?;
    let address = socket.local_addr().ok()?;
    let ip = address.ip();
    if ip.is_loopback() || ip.is_unspecified() {
        None
    } else {
        Some(format!("http://{ip}:{port}"))
    }
}

fn open_with_default_app(url: &str) -> std::io::Result<()> {
    if cfg!(target_os = "windows") {
        std::process::Command::new("cmd")
            .args(["/C", "start", "", url])
            .status()?;
    } else if cfg!(target_os = "macos") {
        std::process::Command::new("open").arg(url).status()?;
    } else {
        std::process::Command::new("xdg-open").arg(url).status()?;
    }
    Ok(())
}
