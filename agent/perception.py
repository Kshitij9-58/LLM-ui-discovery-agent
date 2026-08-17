"""
Perception Layer
===================
Turns a live Playwright page (possibly inside a frameset) into a compact,
LLM-readable observation, and provides the low-level element resolution that
both the discovery loop and the replay engine use.

Design choice: accessibility-tree-first, not raw DOM, and not screenshot+coordinates
as the primary channel.

Why: the brief's glossary calls out that the accessibility tree is "often more
stable than raw markup, and available on desktop apps too." That second half
matters for the multi-surface story (REPORT.md section 4) -- an approach built
around accessibility roles and accessible names generalizes toward native desktop
automation (which also exposes an accessibility tree, e.g. via UIA/AT-SPI) in a way
that CSS-selector-based automation does not. Screenshots are captured too, but only
as supporting evidence (Section 3.5) and as a secondary grounding signal when the
accessibility tree under-describes a control -- not as the primary action-targeting
mechanism, because pixel coordinates are the least stable thing to persist into a
replayable artifact (they break on any resize/zoom/DPI change).

Frames: the mock target uses a classic <frameset> (nav frame + content frame).
Playwright exposes frames as a flat list off the Page; we tag each interactive
element with its owning frame's name so locators and actions can be replayed
against the correct frame later.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from playwright.sync_api import Page, Frame


INTERACTIVE_TAGS = {"a", "button", "input", "select", "textarea"}


@dataclass
class ElementInfo:
    frame_name: str
    tag: str
    role: str
    accessible_name: str
    element_id: Optional[str]
    css_path: str
    text: str
    input_type: Optional[str] = None
    href: Optional[str] = None
    visible: bool = True


@dataclass
class Observation:
    url: str
    title: str
    frame_names: list[str]
    elements: list[ElementInfo] = field(default_factory=list)
    page_text_excerpt: str = ""

    def to_llm_text(self) -> str:
        """Compact, numbered rendering the LLM reasons over and refers to by index."""
        lines = [f"URL: {self.url}", f"Title: {self.title}", ""]
        if len(self.frame_names) > 1:
            lines.append(f"Frames present: {self.frame_names}")
            lines.append("")
        lines.append("Visible interactive elements:")
        for i, el in enumerate(self.elements):
            loc = f"[frame={el.frame_name}] " if el.frame_name != "main" else ""
            extra = f" type={el.input_type}" if el.input_type else ""
            idv = f" id={el.element_id}" if el.element_id else ""
            lines.append(
                f"  {i}: {loc}<{el.tag}{extra}> role={el.role} name=\"{el.accessible_name}\" "
                f"text=\"{el.text[:60]}\"{idv}"
            )
        lines.append("")
        lines.append("Visible page text (excerpt):")
        lines.append(self.page_text_excerpt[:1500])
        return "\n".join(lines)


def _extract_frame(frame: Frame, frame_name: str) -> list[ElementInfo]:
    """Pull a flat list of interactive elements out of one frame via JS eval.

    We use the DOM here (not a true OS accessibility API, which Playwright's Python
    sync API doesn't expose directly) but we compute role/accessible-name using the
    same rules browsers use to build the accessibility tree (explicit ARIA role,
    else implicit role from tag; accessible name from label/aria-label/text content
    in that priority order) -- so the signal we key on is the accessibility-tree
    projection of the DOM, not raw tag/class/id soup.
    """
    try:
        raw = frame.evaluate(
            """
            () => {
              function accessibleName(el) {
                if (el.getAttribute('aria-label')) return el.getAttribute('aria-label');
                if (el.id) {
                  const lbl = document.querySelector(`label[for="${el.id}"]`);
                  if (lbl) return lbl.innerText.trim();
                }
                const parentLabel = el.closest('label');
                if (parentLabel) return parentLabel.innerText.trim();
                // legacy pattern: a <td id="..."> data cell whose preceding sibling
                // cell in the same row is its label, e.g. <td>Savings Balance</td><td id="...">$4821.63</td>
                if (el.tagName === 'TD' && el.previousElementSibling && el.previousElementSibling.tagName === 'TD') {
                  return el.previousElementSibling.innerText.trim();
                }
                if (el.tagName === 'INPUT' || el.tagName === 'SELECT' || el.tagName === 'TEXTAREA') {
                  // legacy pattern: preceding table cell as an implicit label
                  const td = el.closest('td');
                  if (td && td.previousElementSibling) {
                    return td.previousElementSibling.innerText.trim();
                  }
                  // legacy pattern: bare "Label: <input>" with no wrapping element --
                  // walk back over immediately preceding sibling text/anchor nodes.
                  let node = el.previousSibling;
                  let collected = '';
                  while (node) {
                    if (node.nodeType === Node.TEXT_NODE) collected = node.textContent.trim() + ' ' + collected;
                    else if (node.nodeType === Node.ELEMENT_NODE && node.tagName === 'BR') break;
                    else break;
                    node = node.previousSibling;
                  }
                  if (collected.trim()) return collected.trim();
                  // do NOT fall back to el.value for text/password inputs --
                  // that would report the CURRENT VALUE as if it were a stable
                  // accessible name, which is wrong (and a redaction hazard for
                  // password fields specifically).
                  if (el.tagName === 'INPUT' && (el.type === 'text' || el.type === 'password')) return '';
                }
                return (el.innerText || (el.tagName === 'INPUT' && (el.type === 'submit' || el.type === 'button') ? el.value : '') || '').trim();
              }
              function implicitRole(el) {
                const tag = el.tagName.toLowerCase();
                if (tag === 'a' && el.hasAttribute('href')) return 'link';
                if (tag === 'button') return 'button';
                if (tag === 'input') {
                  const t = (el.getAttribute('type') || 'text').toLowerCase();
                  if (t === 'submit' || t === 'button') return 'button';
                  if (t === 'password') return 'textbox(password)';
                  return 'textbox';
                }
                if (tag === 'select') return 'combobox';
                if (tag === 'textarea') return 'textbox';
                return tag;
              }
              const nodes = Array.from(document.querySelectorAll('a,button,input,select,textarea'));
              // Data cells: legacy apps commonly render read-only values (balances, IDs,
              // statuses) as plain table cells with a stable id and no interactive role
              // at all. These aren't "interactive elements" but they ARE the extraction
              // targets a capability needs to read -- treat an id-bearing <td>/<span> as
              // a 'text' role element so the perception layer surfaces it the same way
              // it surfaces a button or input, with its accessible name coming from the
              // preceding label cell just like a form field would.
              const dataCells = Array.from(document.querySelectorAll('td[id], span[id]'))
                .filter(el => !el.querySelector('a,button,input,select,textarea')); // skip cells that just wrap a control
              const allNodes = nodes.concat(dataCells);
              return allNodes.map(el => {
                const rect = el.getBoundingClientRect();
                const visible = !!(rect.width || rect.height) && getComputedStyle(el).visibility !== 'hidden';
                let cssPath = el.tagName.toLowerCase();
                if (el.id) cssPath += '#' + el.id;
                else if (el.name) cssPath += `[name="${el.name}"]`;
                const isDataCell = el.tagName === 'TD' || el.tagName === 'SPAN';
                const isPlainControl = !isDataCell;
                return {
                  tag: el.tagName.toLowerCase(),
                  role: isDataCell ? 'text' : (el.getAttribute('role') || implicitRole(el)),
                  name: isDataCell ? accessibleName(el) : accessibleName(el),
                  id: el.id || null,
                  cssPath: cssPath,
                  text: (el.innerText || el.value || '').trim(),
                  inputType: el.tagName === 'INPUT' ? (el.getAttribute('type') || 'text') : null,
                  href: el.getAttribute('href') || null,
                  visible: visible,
                };
              });
            }
            """
        )
    except Exception:
        return []

    out = []
    for r in raw:
        if not r["visible"]:
            continue
        out.append(
            ElementInfo(
                frame_name=frame_name,
                tag=r["tag"],
                role=r["role"],
                accessible_name=r["name"] or "",
                element_id=r["id"],
                css_path=r["cssPath"],
                text=r["text"] or "",
                input_type=r["inputType"],
                href=r["href"],
                visible=r["visible"],
            )
        )
    return out


def observe(page: Page) -> Observation:
    elements: list[ElementInfo] = []
    frame_names = []

    for fr in page.frames:
        if fr == page.main_frame:
            fname = "main"
        else:
            fname = fr.name or (fr.url.split("/")[-1] if fr.url else "unnamed")
        frame_names.append(fname)
        elements.extend(_extract_frame(fr, fname))

    try:
        body_text = page.evaluate(
            "() => document.body ? document.body.innerText : ''"
        )
    except Exception:
        body_text = ""

    # framesets have no body text on the top document; grab all frames' text too
    if not body_text.strip():
        chunks = []
        for fr in page.frames:
            try:
                t = fr.evaluate("() => document.body ? document.body.innerText : ''")
                if t:
                    chunks.append(t)
            except Exception:
                pass
        body_text = "\n---\n".join(chunks)

    return Observation(
        url=page.url,
        title=page.title(),
        frame_names=frame_names,
        elements=elements,
        page_text_excerpt=body_text.strip(),
    )


def resolve_frame(page: Page, frame_name: Optional[str]) -> "Page | Frame":
    if not frame_name or frame_name == "main":
        return page
    for fr in page.frames:
        candidate = fr.name or (fr.url.split("/")[-1] if fr.url else "")
        if candidate == frame_name:
            return fr
    # fall back to main if the named frame vanished (e.g. navigated out of frameset)
    return page