"use strict";
const el = id => document.getElementById(id);
const local = location.hostname === "127.0.0.1";
let token = "", signature = "", phonePassword = "", submitting = false;
let available = [];
el("phone-tools").hidden = !local;

async function request(path, data) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 8000);
  try {
    const response = await fetch(path, data === undefined ? {cache: "no-store", signal: controller.signal} : {
      method: "POST", signal: controller.signal,
      headers: {"Content-Type": "application/json", "X-Cloudplay-Token": token},
      body: JSON.stringify(data)
    });
    if (!response.ok) throw new Error("Setup is busy or the request was invalid. Check your details and retry.");
    return await response.json();
  } finally {
    clearTimeout(timer);
  }
}

function showNetworks(networks) {
  const next = JSON.stringify(networks);
  if (signature === next) return;
  signature = next;
  available = networks;
  el("networks").replaceChildren(new Option("Enter a network below", ""));
  networks.forEach((network, i) => {
    const option = new Option(`${network.ssid} — ${network.signal}% (${network.security})`, String(i));
    option.disabled = network.security === "unsupported";
    el("networks").add(option);
  });
}

el("networks").addEventListener("change", () => {
  const network = available[Number(el("networks").value)];
  if (el("networks").value !== "" && network) {
    el("ssid").value = network.ssid;
    el("security").value = network.security;
    el("password").value = "";
  }
});
el("security").addEventListener("change", () => {
  if (["open", "owe"].includes(el("security").value)) el("password").value = "";
});

async function submit(path, data) {
  if (submitting) return;
  submitting = true;
  el("connect").disabled = el("phone").disabled = true;
  el("error").textContent = "";
  try {
    token = (await request("/api/token")).token;
    const result = await request(path, data);
    if (!result.accepted) throw new Error("Setup is busy. Please wait.");
    el("status").textContent = "Connecting… The phone hotspot may disconnect. Watch the TV for progress.";
    el("password").value = "";
  } catch (error) {
    el("error").textContent = error.message;
  } finally {
    submitting = false;
  }
}

el("wifi").addEventListener("submit", event => {
  event.preventDefault();
  submit("/api/connect", {
    country: el("country").value, ssid: el("ssid").value,
    password: el("password").value, security: el("security").value,
    hidden: el("hidden").checked
  });
});
el("phone").addEventListener("click", () => {
  submit("/api/phone", {country: el("country").value});
});

async function poll() {
  try {
    if (!token) token = (await request("/api/token")).token;
    const state = await request("/api/status");
    if (el("country").options.length === 1) {
      Object.entries(state.countries).sort((a, b) => a[1].localeCompare(b[1])).forEach(
        ([code, name]) => el("country").add(new Option(`${name} (${code})`, code)));
      if (state.country) el("country").value = state.country;
    }
    el("wireless").hidden = !state.has_wifi || state.connected;
    el("wired").hidden = state.has_wifi || ["checking", "error"].includes(state.phase) || state.connected;
    const waiting = submitting || state.phase !== "setup";
    el("connect").disabled = waiting;
    el("phone").disabled = waiting;
    if (!submitting) el("error").textContent = state.error;
    el("status").textContent = state.connected
      ? (local ? "Connected. Opening GeForce NOW…" : "Connected. Continue on the TV.")
      : state.phase === "connecting" ? "Applying network settings…"
      : state.phase === "checking" ? "Checking Ethernet and saved Wi-Fi…"
      : state.phase === "error" ? "Network setup needs attention."
      : state.has_wifi ? "Connect Ethernet or choose your Wi-Fi network." : "Waiting for Ethernet…";
    showNetworks(state.networks);
    if (local) {
      el("phone-details").hidden = !state.phone_password;
      if (state.phone_password && state.phone_password !== phonePassword) {
        phonePassword = state.phone_password;
        el("qr").src = "/phone.svg";
        el("phone-ssid").textContent = state.phone_ssid;
        el("phone-password").textContent = state.phone_password;
      }
    }
  } catch (_) {
    token = "";
    el("status").textContent = local ? "Waiting for the network setup service…"
      : "The hotspot may have disconnected to apply your settings. Check the TV; reconnect using its new code if needed.";
  }
  setTimeout(poll, 1500);
}
poll();
