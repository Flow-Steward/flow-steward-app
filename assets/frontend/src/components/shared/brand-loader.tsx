import { DsLoader } from "./ds-loader";

/** Compatibility entrypoint for the published decorative wide block Loader. */
export function BrandLoader() {
  return <span aria-hidden="true" className="fs-brand-loader"><DsLoader variant="wide-block" /></span>;
}
