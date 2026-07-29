// "Sign in with Google" — Drive as the version store, no keys or terminal.
let googleState = null;

async function loadGoogle() {
  try {
    googleState = await api("/api/google/status");
    renderGoogle();
  } catch { /* agent starting */ }
}

function renderGoogle() {
  const g = googleState;
  if (!g) return;
  const st = $("googleState");
  const uri = $("redirectUri");
  if (uri) uri.textContent = g.redirect_uri;

  // When the build ships its own client (the normal case for anyone you send
  // the app to), the Google Cloud setup box is irrelevant — hide it entirely.
  $("googleSetup").classList.toggle("hidden", !!g.bundled);

  if (!g.configured) {
    st.className = "gstate";
    st.textContent = "Google access isn't set up on this computer yet — open the setup box below.";
    $("googleSignIn").disabled = true;
    $("googleSetup").open = true;
  } else if (g.signed_in) {
    st.className = "gstate ok";
    st.textContent = "✓ Signed in" + (g.email ? " as " + g.email : "");
    $("googleSignIn").classList.add("hidden");
    $("googleSignOut").classList.remove("hidden");
    $("googleSetup").open = false;
  } else {
    st.className = "gstate";
    st.textContent = "Not signed in.";
    $("googleSignIn").disabled = false;
    $("googleSignIn").classList.remove("hidden");
    $("googleSignOut").classList.add("hidden");
  }
}

async function googleSignIn() {
  try {
    const { url } = await api("/api/google/signin", { method: "POST", body: "{}" });
    window.open(url, "_blank", "noopener");
    toast("Approve access in the tab that just opened", "");
    // Poll until the callback lands, then reflect it.
    let tries = 0;
    const t = setInterval(async () => {
      tries++;
      await loadGoogle();
      if (googleState?.signed_in || tries > 60) {
        clearInterval(t);
        if (googleState?.signed_in) toast("Signed in to Google", "ok");
      }
    }, 2000);
  } catch (e) {
    toast(e.message, "err");
  }
}

async function googleSaveClient() {
  const id = $("gClientId").value.trim();
  const secret = $("gClientSecret").value.trim();
  if (!id || !secret) return toast("Enter both the client ID and secret", "err");
  try {
    googleState = await api("/api/google/setup", {
      method: "POST", body: JSON.stringify({ client_id: id, client_secret: secret }),
    });
    renderGoogle();
    toast("Google access saved — now press Sign in with Google", "ok");
  } catch (e) {
    toast(e.message, "err");
  }
}

async function googleSignOut() {
  try {
    googleState = await api("/api/google/signout", { method: "POST", body: "{}" });
    renderGoogle();
    toast("Signed out of Google", "");
  } catch (e) {
    toast(e.message, "err");
  }
}

$("googleSignIn").onclick = googleSignIn;
$("googleSignOut").onclick = googleSignOut;
$("googleSave").onclick = googleSaveClient;

loadGoogle();
