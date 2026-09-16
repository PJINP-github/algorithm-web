const token = localStorage.getItem("portal_token") || "";
const role = localStorage.getItem("portal_role") || "";
if (!token) window.location.assign("/");
document.addEventListener("DOMContentLoaded", () => {
    document.querySelector("#session-user").textContent = "";
    document.querySelector("#workspace-button").addEventListener("click", () => {
        window.location.assign("/workspace");
    });
    document.querySelector("#dependency-button").addEventListener("click", () => {
        window.location.assign("/dependencies");
    });
    const documentButton = document.querySelector("#document-button");
    if (role === "管理员") {
        documentButton.classList.remove("disabled");
        documentButton.querySelector("span").textContent = "查看文件状态并批量清理";
        documentButton.addEventListener("click", () => window.location.assign("/file-description"));
    } else {
        documentButton.querySelector("span").textContent = "仅管理员开放";
    }
    document.querySelector("#exit-button").addEventListener("click", logout);
});
async function logout() {
    await fetch("/api/logout", {
        method: "POST",
        headers: { Authorization: `Bearer ${token}` },
    }).catch(() => {});
    localStorage.removeItem("portal_token");
    localStorage.removeItem("portal_user");
    localStorage.removeItem("portal_role");
    window.location.assign("/");
}
