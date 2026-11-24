// Fills the browser plotter's form with this box's keys on a fresh session.
(async () => {
  const form = document.querySelector("#config");
  if (!form) return;
  const box = await (await fetch("/api/box")).json();
  if (!box.key) return;
  // The page restores its own session first; give it a moment, then only fill empty fields.
  await new Promise((r) => setTimeout(r, 900));
  const f = form.elements;
  const other = f.farmerKey.value && f.farmerKey.value !== box.key.farmer_pk;
  if (!f.farmerKey.value) {
    f.farmerKey.value = box.key.farmer_pk;
    f.poolKey.value = box.key.pool_pk;
    f.poolMode.value = "pk";
    if (f.network && [...(f.network.options ?? [])].some((o) => o.value === "testnet")) f.network.value = "testnet";
    const dir = form.querySelector('[name="output"][value="dir"]');
    if (dir && !dir.disabled) dir.checked = true;
    for (const t of ["input", "change"]) form.dispatchEvent(new Event(t, { bubbles: true }));
  }
  const used = box.plots.filter((p) => p.kind === "v2").reduce((a, p) => a + p.size, 0) / 1e9;
  const note = document.createElement("p");
  note.style.cssText = "margin:0;padding:10px 16px;background:#13241a;color:#e4ebe3;font:14px/1.45 system-ui,sans-serif";
  note.textContent = other
    ? "The farmer key in this form is not this box's key, so these plots will not farm here. Open a new tab to start over."
    : `Keys for ${box.network} are filled in. Save to the box's plots folder and the plots farm by themselves, or add downloaded files on the dashboard. ${used.toFixed(1)} of ${box.v2_limit_gb} GB v2 allowance used. Keep k at 28.`;
  document.body.prepend(note);
})();
