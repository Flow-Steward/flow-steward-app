import { useId, type CSSProperties } from "react";

/*
 * The Flow Mark, inline rather than an <img> so reduced motion can reach it.
 * Paths and gradient are the approved FS_LOGO_GR file's (design-system/assets/
 * brand/flow-steward-logo.svg); this keeps the mark's 57-unit box, y 14 to 71.
 *
 * Motion is the design system's: the geometry holds still and only the gradient
 * flows. Reduced motion cannot stop SMIL with CSS, so it swaps the fill for an
 * identical still gradient. The wordmark is text beside it (sidebar.md).
 */

const MARK_PATHS = [
  "M99.1783 32.7287C91.9218 32.7287 87.973 41.1899 92.4959 46.7999C96.8485 52.2039 105.758 50.3619 107.522 43.627C109.025 38.1957 104.883 32.695 99.2203 32.7287H99.1783Z",
  "M84.953 50.1791C76.9712 48.4486 70.9553 44.996 70.5285 36.4022C70.2467 33.183 71.0415 29.4991 70.9827 26.2483C70.7703 14.8496 56.4278 9.68744 49.0052 18.3211C45.0227 22.9302 45.969 27.6844 46.4273 33.3113C47.1969 42.2099 41.7593 48.4465 33.5567 49.2035C25.9491 49.0163 19.4602 43.2718 19.3551 35.2584C19.1932 32.302 19.9459 29.3624 19.374 26.5132C18.411 21.3827 13.1647 17.8817 8.09305 18.8342C-1.70553 20.5311 -2.97136 34.2154 6.30365 37.7354C8.44841 38.5491 10.9926 38.8414 13.3077 39.5941C18.4004 41.0681 22.158 44.8151 22.9696 50.0446C24.1724 55.4422 21.6092 61.5547 24.9041 66.3552C31.4435 75.4872 44.8524 70.1527 43.9525 59.0252C43.8978 56.9772 43.7275 54.8976 43.8999 52.8958C45.2288 34.3395 74.1578 36.4485 73.1169 55.1037C72.9866 59.2607 72.4504 63.9203 75.4173 67.2425C80.9074 74.0301 92.384 70.9917 93.6161 62.2066C94.5224 56.4199 90.5946 51.4302 84.993 50.1939L84.951 50.1812L84.953 50.1791Z",
  "M114.529 18.8488C110.631 18.8173 107.883 22.7872 109.195 26.4354C110.629 30.6954 116.512 31.5302 119.13 27.9094C121.853 24.1897 119.222 18.8362 114.571 18.8488H114.529Z",
];

const STOPS = [
  ["0", "#1268C9"],
  ["0.34", "#1B8DBA"],
  ["0.64", "#2BA38C"],
  ["1", "#57C79E"],
] as const;

function Gradient({ id, flowing }: Readonly<{ id: string; flowing: boolean }>) {
  return (
    <linearGradient gradientUnits="userSpaceOnUse" id={id} x1="-55" x2="150" y1="4" y2="84">
      {STOPS.map(([offset, color]) => (
        <stop key={offset} offset={offset} stopColor={color} />
      ))}
      {flowing ? (
        <animateTransform
          attributeName="gradientTransform"
          calcMode="spline"
          dur="6s"
          keySplines="0.42 0 0.58 1; 0.42 0 0.58 1"
          keyTimes="0;0.5;1"
          repeatCount="indefinite"
          type="translate"
          values="0 0; 64 18; 0 0"
        />
      ) : null}
    </linearGradient>
  );
}

export function FlowStewardMark({
  className,
  testId,
  flowing = true,
}: Readonly<{
  className: string;
  testId?: string;
  /** False holds the gradient still, as on a catalog card's logo tile. */
  flowing?: boolean;
}>) {
  // useId returns ":r0:"; colons are not safe inside url(#...).
  const base = `fs-logo-${useId().replace(/[^a-zA-Z0-9_-]/g, "")}`;
  const flow = `${base}-flow`;
  const still = `${base}-still`;
  return (
    <svg
      aria-hidden="true"
      className={className}
      data-testid={testId}
      focusable="false"
      style={{ "--fs-logo-still-fill": `url(#${still})` } as CSSProperties}
      viewBox="0 14 121 57"
      xmlns="http://www.w3.org/2000/svg"
    >
      <defs>
        <Gradient flowing={flowing} id={flow} />
        <Gradient flowing={false} id={still} />
      </defs>
      <g className="fs-flow-mark__shape" fill={`url(#${flow})`} id={`${base}-mark`}>
        {MARK_PATHS.map((d) => (
          <path d={d} key={d.slice(0, 16)} />
        ))}
      </g>
    </svg>
  );
}
