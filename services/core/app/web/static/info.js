// Escape closes an open info tooltip (WCAG 1.4.13: dismissible without moving the pointer or focus).
// The tooltip opens with CSS alone (hover, keyboard focus, tap); this only adds the dismissal. It stays
// closed until both the pointer and the focus have left its button, so it never pops back on its own.
"use strict";
document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape") return;
  document.querySelectorAll(".info:hover, .info:focus-within").forEach((info) => info.classList.add("info-dismissed"));
});
function reopenLater(event) {
  const info = event.target instanceof Element ? event.target.closest(".info") : null;
  if (!info) return;
  setTimeout(() => {
    if (!info.matches(":hover, :focus-within")) info.classList.remove("info-dismissed");
  });
}
document.addEventListener("mouseout", reopenLater);
document.addEventListener("focusout", reopenLater);
