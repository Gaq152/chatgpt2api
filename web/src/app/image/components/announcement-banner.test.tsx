import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { AnnouncementBanner } from "./announcement-banner";

describe("AnnouncementBanner", () => {
  it("shows the announcement when enabled with non-empty content", () => {
    render(<AnnouncementBanner announcement={{ enabled: true, message: "系统维护中\n请稍后再试" }} />);

    expect(screen.getByText("公告")).toBeInTheDocument();
    expect(screen.getByText(/系统维护中/)).toBeInTheDocument();
    expect(screen.getByText(/请稍后再试/)).toBeInTheDocument();
  });

  it("does not render when disabled", () => {
    const { container } = render(<AnnouncementBanner announcement={{ enabled: false, message: "系统维护中" }} />);

    expect(container).toBeEmptyDOMElement();
  });

  it("does not render when enabled but content is blank", () => {
    const { container } = render(<AnnouncementBanner announcement={{ enabled: true, message: "   \n  " }} />);

    expect(container).toBeEmptyDOMElement();
  });
});
