use axum::extract::{Form, State};
// 引入 State 提取器和 Form 提取器，分别取状态和表单数据
use crate::models::NewTodo;
// 引入 NewTodo 结构体，用于反序列化表单 body
pub async fn add(
    State(state): State<crate::state::AppState>,
    Form(f): Form<NewTodo>,
) -> maud::Markup {
    // 提取状态和表单，返回 HTML
    crate::db::call(state.clone(), move |c| {
        c.execute("INSERT INTO todos (title) VALUES (?1)", [&f.title])
            .unwrap()
    })
    .await;
    // 参数化插入新待办，?1 是占位符防止 SQL 注入
    super::list::list(State(state)).await
    // 插入后复用 list handler 重新查询完整列表，返回给 HTMX 替换 #list
}
