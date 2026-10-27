import type { ComponentProps, ReactNode } from "react";

import { AnimatePresence, MotionConfig, motion } from "motion/react";
import { useLocation, useOutlet } from "react-router";

import { Button } from "@/components/ui/button";
import {
  AUTH_LAYOUT_TRANSITION,
  AUTH_ROUTE_DEPTH,
  authContentVariants,
  authRouteVariants,
} from "@/pages/auth/public-auth-motion-config";

const MotionButtonBase = motion.create(Button);

export function MotionButton(props: Readonly<ComponentProps<typeof MotionButtonBase>>) {
  return <MotionButtonBase {...props} />;
}

export function PublicAuthMotionLayout() {
  const location = useLocation();
  const outlet = useOutlet();
  const currentDepth = AUTH_ROUTE_DEPTH[location.pathname] ?? 0;
  const direction = currentDepth === 0 ? -1 : 1;

  return (
    <MotionConfig reducedMotion="user">
      <AnimatePresence custom={direction} initial={false} mode="wait">
        <motion.div
          animate="center"
          className="fs-public-auth-route"
          custom={direction}
          exit="exit"
          initial="enter"
          key={location.pathname}
          variants={authRouteVariants}
        >
          {outlet}
        </motion.div>
      </AnimatePresence>
    </MotionConfig>
  );
}

export function AuthContentPresence({
  children,
  className,
  stateKey,
}: Readonly<{
  children: ReactNode;
  className?: string;
  stateKey: string;
}>) {
  return (
    <AnimatePresence initial={false} mode="wait">
      <motion.div
        animate="center"
        className={className}
        exit="exit"
        initial="enter"
        key={stateKey}
        layout
        transition={{ layout: AUTH_LAYOUT_TRANSITION }}
        variants={authContentVariants}
      >
        {children}
      </motion.div>
    </AnimatePresence>
  );
}
