// Review page helpers. Loaded from this origin only (the CSP allows no inline script).
// Without scripts the page still works: the account picker is a plain grouped select, the decision
// panel shows each action's fields with CSS, and the server checks everything.
"use strict";

// ---- Account search: turns <select data-combobox> into an ARIA 1.2 combobox -----------------------
// The select stays in the form (hidden) and keeps the chosen value, so what is posted doesn't change.
function accountCombobox(select) {
  const id = select.id;
  const groups = [...select.querySelectorAll("optgroup")].map((g) => ({
    label: g.label,
    options: [...g.querySelectorAll("option")].map((o) => ({ value: o.value, text: o.textContent.trim() })),
  }));

  const wrap = document.createElement("div");
  wrap.className = "combo";
  const input = document.createElement("input");
  input.type = "text";
  input.id = id + "-search";
  input.setAttribute("role", "combobox");
  input.setAttribute("aria-autocomplete", "list");
  input.setAttribute("aria-expanded", "false");
  input.setAttribute("aria-controls", id + "-list");
  input.autocomplete = "off";
  input.spellcheck = false;
  input.placeholder = "Search accounts by name or type";
  if (select.hasAttribute("aria-describedby")) input.setAttribute("aria-describedby", select.getAttribute("aria-describedby"));
  const list = document.createElement("ul");
  list.id = id + "-list";
  list.className = "combo-list";
  list.setAttribute("role", "listbox");
  list.setAttribute("aria-label", "Accounts");
  list.hidden = true;
  wrap.append(input, list);
  select.hidden = true;
  select.after(wrap);
  const label = document.querySelector(`label[for="${id}"]`);
  if (label) label.htmlFor = input.id;

  const textOf = (value) => {
    for (const g of groups) for (const o of g.options) if (o.value === value) return o.text;
    return "";
  };
  input.value = textOf(select.value);
  let shown = []; // option elements currently listed, in order
  let active = -1;

  function render() {
    const words = input.value.toLowerCase().split(/\s+/).filter(Boolean);
    const typed = input.value !== textOf(select.value); // the current choice shown: list everything
    list.replaceChildren();
    shown = [];
    groups.forEach((g, gi) => {
      const hits = g.options.filter((o) => !typed || words.every((w) => (o.text + " " + g.label).toLowerCase().includes(w)));
      if (!hits.length) return;
      const li = document.createElement("li");
      li.className = "combo-group";
      li.setAttribute("role", "presentation");
      const name = document.createElement("span");
      name.id = `${id}-g${gi}`;
      name.textContent = g.label;
      const ul = document.createElement("ul");
      ul.setAttribute("role", "group");
      ul.setAttribute("aria-labelledby", name.id);
      for (const o of hits) {
        const opt = document.createElement("li");
        opt.id = `${id}-o${shown.length}`;
        opt.className = "combo-option" + (o.value === select.value ? " current" : "");
        opt.setAttribute("role", "option");
        opt.setAttribute("aria-selected", "false");
        opt.dataset.value = o.value;
        opt.textContent = o.text;
        opt.addEventListener("mousedown", (e) => { e.preventDefault(); choose(o.value); });
        ul.append(opt);
        shown.push(opt);
      }
      li.append(name, ul);
      list.append(li);
    });
    if (!shown.length) {
      const none = document.createElement("li");
      none.className = "combo-empty";
      none.setAttribute("role", "presentation");
      none.textContent = "No account matches. Try another word.";
      list.append(none);
    }
    const chosen = shown.findIndex((o) => o.dataset.value === select.value);
    setActive(shown.length ? Math.max(chosen, 0) : -1);
  }

  function setActive(i) {
    if (shown[active]) shown[active].setAttribute("aria-selected", "false");
    active = i;
    if (shown[active]) {
      shown[active].setAttribute("aria-selected", "true");
      input.setAttribute("aria-activedescendant", shown[active].id);
      shown[active].scrollIntoView({ block: "nearest" });
    } else {
      input.removeAttribute("aria-activedescendant");
    }
  }

  function open() {
    if (!list.hidden) return;
    list.hidden = false;
    input.setAttribute("aria-expanded", "true");
    render();
  }

  function close() {
    list.hidden = true;
    input.setAttribute("aria-expanded", "false");
    input.removeAttribute("aria-activedescendant");
  }

  function choose(value) {
    select.value = value;
    select.dispatchEvent(new Event("change", { bubbles: true }));
    input.value = textOf(value);
    close();
  }

  input.addEventListener("focus", () => input.select());
  input.addEventListener("click", open);
  input.addEventListener("input", () => { list.hidden = false; input.setAttribute("aria-expanded", "true"); render(); });
  input.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      if (list.hidden) { open(); return; }
      if (!shown.length) return;
      const step = e.key === "ArrowDown" ? 1 : -1;
      setActive((active + step + shown.length) % shown.length);
    } else if (e.key === "Enter") {
      if (!list.hidden) {
        e.preventDefault(); // choosing an account never submits the form
        if (shown[active]) choose(shown[active].dataset.value);
      }
    } else if (e.key === "Escape") {
      if (!list.hidden) { e.preventDefault(); close(); input.value = textOf(select.value); }
    } else if (e.key === "Tab") {
      close();
    }
  });
  // Leaving the field with half-typed text puts back the chosen account: what is shown is what is posted
  input.addEventListener("blur", () => { close(); input.value = textOf(select.value); });
}

document.addEventListener("DOMContentLoaded", () => {
  for (const select of document.querySelectorAll("select[data-combobox]")) accountCombobox(select);

  // The confirm button waits for what the chosen action still needs: an action at all (possible
  // fraud: nothing is pre-selected), and every field marked data-required-for="<action>" (an account
  // to choose, the suggested dates to accept). Without scripts the button stays enabled and the
  // server refuses what is missing.
  const filled = (el) => {
    if (el.type === "radio") return !!el.form.querySelector(`input[name="${el.name}"]:checked`); // a group: one picked
    return el.type === "checkbox" ? el.checked : el.value !== "";
  };
  for (const button of document.querySelectorAll("button[data-needs-action]")) {
    const form = button.form;
    const update = () => {
      const chosen = form.querySelector('input[name="action"]:checked');
      const needed = chosen ? [...form.querySelectorAll(`[data-required-for~="${chosen.value}"]`)] : [];
      button.disabled = !chosen || !needed.every(filled);
    };
    form.addEventListener("change", update);
    update();
  }

  // Rejecting is irreversible: ask first
  for (const form of document.querySelectorAll("form[data-confirm-reject]")) {
    form.addEventListener("submit", (event) => {
      const action = form.querySelector('input[name="action"]:checked');
      if (action && action.value === "reject" && !window.confirm(form.dataset.confirmReject)) event.preventDefault();
    });
  }
  for (const form of document.querySelectorAll("form[data-confirm]")) {
    form.addEventListener("submit", (event) => {
      if (!window.confirm(form.dataset.confirm)) event.preventDefault();
    });
  }
});
