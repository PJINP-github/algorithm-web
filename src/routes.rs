pub fn build(state: crate::state::AppState) -> axum::Router {
    axum::Router::new()
        .route("/", axum::routing::get(index))
        .route("/style.css", axum::routing::get(style))
        .route("/script.js", axum::routing::get(script))
        .route("/workspace", axum::routing::get(workspace))
        .route("/workspace.css", axum::routing::get(workspace_style))
        .route("/workspace.js", axum::routing::get(workspace_script))
        .route("/features", axum::routing::get(features))
        .route("/features.css", axum::routing::get(features_style))
        .route("/features.js", axum::routing::get(features_script))
        .route("/dependencies", axum::routing::get(dependencies))
        .route("/dependencies.css", axum::routing::get(dependencies_style))
        .route("/dependencies.js", axum::routing::get(dependencies_script))
        .route("/file-description", axum::routing::get(file_description))
        .route(
            "/file-description.css",
            axum::routing::get(file_description_style),
        )
        .route(
            "/file-description.js",
            axum::routing::get(file_description_script),
        )
        .route("/user-icon.svg", axum::routing::get(user_icon))
        .route(
            "/api/register",
            axum::routing::post(crate::handlers::auth::register),
        )
        .route(
            "/api/login",
            axum::routing::post(crate::handlers::auth::login),
        )
        .route(
            "/api/logout",
            axum::routing::post(crate::handlers::auth::logout),
        )
        .route(
            "/api/workspace",
            axum::routing::get(crate::handlers::workspace::catalog),
        )
        .route(
            "/api/model/run",
            axum::routing::post(crate::handlers::workspace::start_job),
        )
        .route(
            "/api/trueno/catalog",
            axum::routing::get(crate::handlers::workspace::trueno_catalog),
        )
        .route(
            "/api/dependencies",
            axum::routing::get(crate::handlers::workspace::dependency_catalog),
        )
        .route(
            "/api/dependencies/download",
            axum::routing::get(crate::handlers::workspace::download_dependency),
        )
        .route(
            "/api/dependencies/open",
            axum::routing::post(crate::handlers::workspace::open_dependency),
        )
        .route(
            "/api/file-description",
            axum::routing::get(crate::handlers::workspace::file_description_catalog),
        )
        .route(
            "/api/file-description/delete",
            axum::routing::post(crate::handlers::workspace::delete_file_description),
        )
        .route(
            "/api/jobs/{id}",
            axum::routing::get(crate::handlers::workspace::job),
        )
        .route(
            "/api/job-control/running",
            axum::routing::get(crate::handlers::workspace::running_jobs),
        )
        .route(
            "/api/job-control/cancel",
            axum::routing::post(crate::handlers::workspace::cancel_jobs),
        )
        .route(
            "/api/files",
            axum::routing::get(crate::handlers::workspace::list_files),
        )
        .route(
            "/api/files/upload",
            axum::routing::post(crate::handlers::workspace::upload)
                .layer(axum::extract::DefaultBodyLimit::max(2 * 1024 * 1024 * 1024)),
        )
        .route(
            "/api/files/local-import",
            axum::routing::post(crate::handlers::workspace::local_import),
        )
        .route(
            "/api/files/download",
            axum::routing::get(crate::handlers::workspace::download),
        )
        .route(
            "/api/files/content",
            axum::routing::get(crate::handlers::workspace::content),
        )
        .route(
            "/api/files/open",
            axum::routing::post(crate::handlers::workspace::open_file),
        )
        .route(
            "/api/files/open-directory",
            axum::routing::post(crate::handlers::workspace::open_directory),
        )
        .route(
            "/api/config",
            axum::routing::get(crate::handlers::workspace::get_config)
                .put(crate::handlers::workspace::update_config),
        )
        .route(
            "/api/admin/codes",
            axum::routing::get(crate::handlers::workspace::list_codes)
                .post(crate::handlers::workspace::create_code),
        )
        .route("/todos", axum::routing::get(crate::handlers::list::list))
        .route("/add", axum::routing::post(crate::handlers::add::add))
        .with_state(state)
}

async fn index() -> axum::response::Html<&'static str> {
    axum::response::Html(include_str!("views/index/index.html"))
}

async fn style() -> impl axum::response::IntoResponse {
    (
        [(axum::http::header::CONTENT_TYPE, "text/css; charset=utf-8")],
        include_str!("views/index/style.css"),
    )
}

async fn script() -> impl axum::response::IntoResponse {
    (
        [(
            axum::http::header::CONTENT_TYPE,
            "application/javascript; charset=utf-8",
        )],
        include_str!("views/index/script.js"),
    )
}

async fn workspace() -> axum::response::Html<&'static str> {
    axum::response::Html(include_str!("views/workspace/workspace.html"))
}

async fn workspace_style() -> impl axum::response::IntoResponse {
    (
        [(axum::http::header::CONTENT_TYPE, "text/css; charset=utf-8")],
        include_str!("views/workspace/workspace.css"),
    )
}

async fn workspace_script() -> impl axum::response::IntoResponse {
    (
        [(
            axum::http::header::CONTENT_TYPE,
            "application/javascript; charset=utf-8",
        )],
        include_str!("views/workspace/workspace.js"),
    )
}

async fn features() -> axum::response::Html<&'static str> {
    axum::response::Html(include_str!("views/features/features.html"))
}

async fn features_style() -> impl axum::response::IntoResponse {
    (
        [(axum::http::header::CONTENT_TYPE, "text/css; charset=utf-8")],
        include_str!("views/features/features.css"),
    )
}

async fn features_script() -> impl axum::response::IntoResponse {
    (
        [(
            axum::http::header::CONTENT_TYPE,
            "application/javascript; charset=utf-8",
        )],
        include_str!("views/features/features.js"),
    )
}

async fn dependencies() -> axum::response::Html<&'static str> {
    axum::response::Html(include_str!("views/dependencies/dependencies.html"))
}

async fn dependencies_style() -> impl axum::response::IntoResponse {
    (
        [(axum::http::header::CONTENT_TYPE, "text/css; charset=utf-8")],
        include_str!("views/dependencies/dependencies.css"),
    )
}

async fn dependencies_script() -> impl axum::response::IntoResponse {
    (
        [(
            axum::http::header::CONTENT_TYPE,
            "application/javascript; charset=utf-8",
        )],
        include_str!("views/dependencies/dependencies.js"),
    )
}

async fn file_description() -> axum::response::Html<&'static str> {
    axum::response::Html(include_str!("views/file_description/file_description.html"))
}

async fn file_description_style() -> impl axum::response::IntoResponse {
    (
        [(axum::http::header::CONTENT_TYPE, "text/css; charset=utf-8")],
        include_str!("views/file_description/file_description.css"),
    )
}

async fn file_description_script() -> impl axum::response::IntoResponse {
    (
        [(
            axum::http::header::CONTENT_TYPE,
            "application/javascript; charset=utf-8",
        )],
        include_str!("views/file_description/file_description.js"),
    )
}

async fn user_icon(
    axum::extract::Query(query): axum::extract::Query<std::collections::HashMap<String, String>>,
) -> impl axum::response::IntoResponse {
    let icon = match query.get("role").map(String::as_str) {
        Some("管理员") => include_str!("../sources/icons/users/管理员用户.svg"),
        Some("普通一级") => include_str!("../sources/icons/users/一级用户.svg"),
        Some("普通二级") => include_str!("../sources/icons/users/二级用户.svg"),
        _ => include_str!("../sources/icons/users/二级用户.svg"),
    };
    (
        [(
            axum::http::header::CONTENT_TYPE,
            "image/svg+xml; charset=utf-8",
        )],
        icon,
    )
}
