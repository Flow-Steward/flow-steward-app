/**
 * What of a rendered Word document may reach the page (ADR 0077).
 *
 * docx-preview builds DOM nodes from the document's XML. Before they are shown,
 * every element outside the HTML, SVG and MathML a document needs is removed
 * with its content, every event handler attribute is dropped, links keep only
 * in-document anchors (an external address stays readable as a tooltip), and
 * images keep only `data:image/…` sources the previewer made itself. The frame
 * these nodes go into runs no script either, so a missed case still runs nothing.
 */

const HTML_ELEMENTS = new Set([
  "a", "article", "b", "br", "caption", "col", "colgroup", "del", "div", "em", "footer", "h1", "h2", "h3", "h4",
  "h5", "h6", "header", "hr", "i", "img", "ins", "li", "mark", "ol", "p", "s", "section", "small", "span",
  "strong", "sub", "sup", "table", "tbody", "td", "tfoot", "th", "thead", "tr", "u", "ul", "wbr",
]);
const SVG_ELEMENTS = new Set(["svg", "g", "rect", "ellipse", "circle", "line", "path", "polyline", "polygon", "image", "text", "tspan", "foreignobject"]);
const MATHML_ELEMENTS = new Set([
  "math", "menclose", "mfrac", "mi", "mn", "mo", "mover", "mpadded", "mphantom", "mroot", "mrow", "ms", "mspace",
  "msqrt", "mstyle", "msub", "msubsup", "msup", "mtable", "mtd", "mtext", "mtr", "munder", "munderover",
]);
const SVG_NS = "http://www.w3.org/2000/svg";
const MATHML_NS = "http://www.w3.org/1998/Math/MathML";
const SAFE_IMAGE = /^data:image\/(?:png|jpe?g|gif|webp|bmp|x-emf|x-wmf|svg\+xml);base64,/i;
const URL_ATTRIBUTES = new Set(["href", "xlink:href", "src"]);
const DROPPED_ATTRIBUTES = new Set(["srcset", "action", "formaction", "background", "poster", "ping", "lowsrc", "dynsrc"]);

function allowedElement(element: Element): boolean {
  const name = element.localName.toLowerCase();
  if (element.namespaceURI === SVG_NS) return SVG_ELEMENTS.has(name);
  if (element.namespaceURI === MATHML_NS) return MATHML_ELEMENTS.has(name);
  return HTML_ELEMENTS.has(name);
}

function cleanUrlAttribute(element: Element, attribute: Attr): void {
  const value = attribute.value.trim();
  const isImage = element.localName.toLowerCase() === "img" || element.localName.toLowerCase() === "image";
  if (isImage) {
    if (!SAFE_IMAGE.test(value)) element.removeAttribute(attribute.name);
    return;
  }
  if (value.startsWith("#")) return;
  element.removeAttribute(attribute.name);
  if (element.localName.toLowerCase() === "a" && /^(?:https?:|mailto:)/i.test(value) && !element.hasAttribute("title")) {
    element.setAttribute("title", value);
  }
}

function cleanAttributes(element: Element): void {
  for (const attribute of Array.from(element.attributes)) {
    const name = attribute.name.toLowerCase();
    if (name.startsWith("on") || DROPPED_ATTRIBUTES.has(name)) {
      element.removeAttribute(attribute.name);
    } else if (URL_ATTRIBUTES.has(name)) {
      cleanUrlAttribute(element, attribute);
    }
  }
}

function cleanTree(node: Node): boolean {
  if (node.nodeType === Node.TEXT_NODE) return true;
  if (node.nodeType !== Node.ELEMENT_NODE) return false;
  const element = node as Element;
  if (!allowedElement(element)) return false;
  cleanAttributes(element);
  for (const child of Array.from(element.childNodes)) {
    if (!cleanTree(child)) child.remove();
  }
  return true;
}

/** The nodes that may be shown, cleaned in place; anything else is dropped. */
export function sanitizeDocxNodes(nodes: readonly Node[]): Node[] {
  return nodes.filter((node) => cleanTree(node));
}

/** The style text docx-preview generated: the CSS of the document's own styles. */
export function docxStyleText(nodes: readonly Node[]): string {
  return nodes
    .filter((node): node is HTMLStyleElement => node.nodeType === Node.ELEMENT_NODE && (node as Element).localName.toLowerCase() === "style")
    .map((node) => node.textContent ?? "")
    .join("\n");
}
