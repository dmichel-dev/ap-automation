// Review page helpers. Loaded from this origin only (the CSP allows no inline script).
"use strict";
document.addEventListener("DOMContentLoaded", () => {
  // Account picker: typing filters the options of the select it names
  for (const input of document.querySelectorAll("input.filter[data-filter-for]")) {
    const select = document.getElementById(input.dataset.filterFor);
    if (!select) continue;
    input.addEventListener("input", () => {
      const words = input.value.toLowerCase().split(/\s+/).filter(Boolean);
      for (const option of select.options) {
        if (!option.value) continue;
        const text = option.textContent.toLowerCase();
        option.hidden = !words.every((w) => text.includes(w));
      }
    });
  }
  // Irreversible actions ask first
  for (const form of document.querySelectorAll("form[data-confirm]")) {
    form.addEventListener("submit", (event) => {
      if (!window.confirm(form.dataset.confirm)) event.preventDefault();
    });
  }
});
