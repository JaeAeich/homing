// Stage panel: opens when HTMX fills it; closes on ×, the scrim or Escape.
// Copy buttons put their command on the clipboard.
(() => {
  const panel = () => document.getElementById("stage-panel");
  const close = () => document.body.classList.remove("panel-open");
  document.addEventListener("htmx:afterSwap", (event) => {
    if (event.detail.target === panel()) {
      document.body.classList.add("panel-open");
    }
  });
  document.addEventListener("click", (event) => {
    const target = event.target.closest("[data-close-panel], [data-copy]");
    if (!target) return;
    if (target.hasAttribute("data-copy")) {
      navigator.clipboard.writeText(target.dataset.copy);
      target.textContent = "Copied";
      return;
    }
    close();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") close();
  });
})();
