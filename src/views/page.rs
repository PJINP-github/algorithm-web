use maud::{DOCTYPE, Markup, html};
// 引入 Maud 的 html 宏、Markup 类型和 DOCTYPE 常量
pub fn render(todos: Vec<String>) -> Markup {
    // 接收待办列表，返回编译期生成的 HTML
    html! {       // html! 宏在编译期展开为字符串拼接，运行时零模板解析开销
        (DOCTYPE) // 输出 <!DOCTYPE html>，Maud 内置常量
        html { head { title { "Todo" } script src="https://unpkg.com/htmx.org@2" {} } }  // 加载 HTMX，提供 hx-post 等属性
        body { ul #list { @for t in &todos { li { (t) } } }  // #list 是 HTMX 的目标容器，后续动态替换这个 ul
               form hx-post="/add" hx-target="#list" { input name="title" {} button { "Add" } } }  // 提交后 HTMX 用响应替换 #list
    }
}
