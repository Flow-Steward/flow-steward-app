import { fireEvent, render, screen } from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router";

import { renderMarkdownBlocks } from "@/pages/extensions/extension-markdown";

describe("markdown links inside the app", () => {
  it("opens an in-app link through the router, without a new tab", () => {
    const router = createMemoryRouter(
      [
        { path: "/extensions/:id", element: <div>{renderMarkdownBlocks("See [run 7](/extensions/acme.store?extension_page=run_detail&p.run_id=7).")}</div> },
      ],
      { initialEntries: ["/extensions/acme.store?extension_page=runs"] },
    );
    render(<RouterProvider router={router} />);

    const link = screen.getByRole("link", { name: "run 7" });
    expect(link).not.toHaveAttribute("target");
    fireEvent.click(link);
    expect(router.state.location.search).toContain("p.run_id=7");
  });

  it("keeps an external link in a new tab", () => {
    render(<div>{renderMarkdownBlocks("[docs](https://example.test/docs)")}</div>);
    expect(screen.getByRole("link", { name: "docs" })).toHaveAttribute("target", "_blank");
  });
});
