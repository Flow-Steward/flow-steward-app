import type { Variants } from "motion/react";

export const AUTH_ENTER_TRANSITION = {
  duration: 0.2,
  ease: [0.16, 1, 0.3, 1],
} as const;

export const AUTH_EXIT_TRANSITION = {
  duration: 0.12,
  ease: [0.4, 0, 1, 1],
} as const;

export const AUTH_FEEDBACK_TRANSITION = {
  duration: 0.16,
  ease: [0.16, 1, 0.3, 1],
} as const;

export const AUTH_LAYOUT_TRANSITION = {
  bounce: 0,
  type: "spring",
  visualDuration: 0.22,
} as const;

export const AUTH_BUTTON_MOTION = {
  transition: {
    bounce: 0,
    type: "spring",
    visualDuration: 0.16,
  },
  whileHover: { transform: "translateY(-1px)" },
  whileTap: { transform: "translateY(0px) scale(0.985)" },
} as const;

export const AUTH_FEEDBACK_MOTION = {
  animate: { opacity: 1, transform: "translateY(0px)" },
  exit: { opacity: 0, transform: "translateY(-4px)" },
  initial: { opacity: 0, transform: "translateY(-4px)" },
  transition: AUTH_FEEDBACK_TRANSITION,
} as const;

export const AUTH_NOTICE_MOTION = {
  animate: { opacity: 1 },
  exit: { opacity: 0 },
  initial: { opacity: 0 },
  transition: AUTH_FEEDBACK_TRANSITION,
} as const;

export const AUTH_ROUTE_DEPTH: Record<string, number> = {
  "/login": 0,
  "/forgot-password": 1,
  "/reset-password": 2,
};

export const authRouteVariants: Variants = {
  enter: (direction: number) => ({
    opacity: 0,
    transform: `translateX(${direction * 8}px)`,
  }),
  center: {
    opacity: 1,
    transform: "translateX(0px)",
    transition: AUTH_ENTER_TRANSITION,
  },
  exit: (direction: number) => ({
    opacity: 0,
    transform: `translateX(${direction * -8}px)`,
    transition: AUTH_EXIT_TRANSITION,
  }),
};

export const authContentVariants: Variants = {
  enter: {
    opacity: 0,
    transform: "translateY(6px) scale(0.995)",
  },
  center: {
    opacity: 1,
    transform: "translateY(0px) scale(1)",
    transition: AUTH_ENTER_TRANSITION,
  },
  exit: {
    opacity: 0,
    transform: "translateY(-4px) scale(0.997)",
    transition: AUTH_EXIT_TRANSITION,
  },
};
