/*
    Logic：
        主要采用原生 JavaScript，
        只有在发送 Ajax 请求是才使用 JQuery
    
    ===
    1、登录注册页面的切换逻辑

    2、Ajax发送及接受响应逻辑
    ===
*/


const getSelector = selector => document.querySelector(selector);


// 登录注册载入

document.addEventListener("DOMContentLoaded", () => {
    const container = getSelector(".container");
    const toSignBtn = getSelector(".toSign");
    const toLoginBtn = getSelector(".toLogin");
    const loginBox = getSelector(".login-box");
    const signBox = getSelector(".sign-box");

    container.classList.add("container-show");

    toSignBtn.addEventListener("click", () => {
        loginBox.classList.add("animate_login");
        signBox.classList.add("animate_sign");
    });

    toLoginBtn.addEventListener("click", () => {
        loginBox.classList.remove("animate_login");
        signBox.classList.remove("animate_sign");
    });

    getSelector(".login-btn").addEventListener("click", () => {
        submitAuth("/api/login", "#login-user", "#login-password", "", "#login-message");
    });

    getSelector(".sign-btn").addEventListener("click", () => {
        submitAuth("/api/register", "#sign-user", "#sign-password", "#sign-code", "#sign-message");
    });
});

async function submitAuth(endpoint, usernameSelector, passwordSelector, codeSelector, messageSelector) {
    const username = getSelector(usernameSelector).value.trim();
    const password = getSelector(passwordSelector).value;
    const authorization_code = codeSelector ? getSelector(codeSelector).value.trim() : "";
    const message = getSelector(messageSelector);

    if (!username || !password) {
        showMessage(message, "请输入用户名和密码", false);
        return;
    }
    if (endpoint === "/api/register" && !authorization_code) {
        showMessage(message, "请输入授权码", false);
        return;
    }

    showMessage(message, "正在处理...", true);

    try {
        const response = await fetch(endpoint, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ username, password, authorization_code })
        });
        const result = await response.json();

        showMessage(message, result.message, response.ok);

        if (!response.ok) {
            return;
        }

        if (endpoint === "/api/register") {
            getSelector("#login-user").value = username;
            getSelector("#login-password").value = password;
            toLogin();
            showMessage(getSelector("#login-message"), "注册成功，请登录", true);
        } else {
            localStorage.setItem("portal_token", result.token);
            localStorage.setItem("portal_user", result.username || username);
            localStorage.setItem("portal_role", result.role || "");
            window.location.assign("/features");
        }
    } catch (error) {
        showMessage(message, "网络错误，请稍后重试", false);
    }
}

function toLogin() {
    getSelector(".login-box").classList.remove("animate_login");
    getSelector(".sign-box").classList.remove("animate_sign");
}

function showMessage(element, text, success) {
    element.textContent = text;
    element.classList.toggle("message-success", success);
    element.classList.toggle("message-error", !success);
}
