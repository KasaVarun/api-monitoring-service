const mode = document.body.dataset.authMode;
const form = document.getElementById("auth-form");
const errorBox = document.getElementById("auth-error");
const submitButton = form.querySelector("button[type='submit']");

function showError(message) {
  errorBox.textContent = message;
  errorBox.classList.toggle("hidden", !message);
}

function safeNextPath() {
  const requested = new URLSearchParams(window.location.search).get("next");
  return requested && requested.startsWith("/") && !requested.startsWith("//")
    ? requested
    : "/";
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  showError("");
  const data = new FormData(form);
  const password = String(data.get("password") || "");
  if (mode === "signup" && password !== String(data.get("confirm_password") || "")) {
    showError("Passwords do not match.");
    return;
  }

  submitButton.disabled = true;
  submitButton.textContent = mode === "signup" ? "Creating account…" : "Logging in…";
  try {
    const response = await fetch(`/api/auth/${mode}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        username: String(data.get("username") || "").trim(),
        password,
      }),
    });
    const payload = await response.json().catch(() => null);
    if (!response.ok) {
      throw new Error(payload?.message || payload?.detail || "Authentication failed");
    }
    window.location.assign(safeNextPath());
  } catch (error) {
    showError(error.message);
  } finally {
    submitButton.disabled = false;
    submitButton.textContent = mode === "signup" ? "Create account" : "Log in";
  }
});
