use axum::extract::State;
// 引入 Axum 的 State 提取器，从路由中取出 AppState
pub async fn list(State(state): State<crate::state::AppState>) -> maud::Markup {
    // 提取 AppState，返回 Maud HTML
    let titles = crate::db::call(state, |c| {
        // 调用 db 模块的通用包装，闭包内是同步 rusqlite 代码
        let mut stmt = c.prepare("SELECT title FROM todos ORDER BY id").unwrap();
        // 预编译 SQL，防止注入并提升重复执行性能
        stmt.query_map([], |r| r.get::<_, String>(0))
            .unwrap()
            .map(|r| r.unwrap())
            .collect()
        // 将查询结果收集为 Vec<String>
    })
    .await;
    // await 等待 spawn_blocking 完成，Tokio worker 全程不被阻塞
    crate::views::page::render(titles)
    // 将待办列表交给 views 模块渲染为完整页面 HTML
}
