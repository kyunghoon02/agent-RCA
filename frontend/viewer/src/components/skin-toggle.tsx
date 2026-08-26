"use client";

import { Palette } from "lucide-react";
import { Button } from "@/components/ui/button";
import { SKINS, SKIN_LABELS, useSkin } from "@/components/skin-provider";
import { cn } from "@/lib/utils";

/** Cycles the visual skin. Colour and corner radius only — never layout. */
export function SkinToggle({ className }: { className?: string }) {
  const { skin, setSkin } = useSkin();
  const next = SKINS[(SKINS.indexOf(skin) + 1) % SKINS.length];

  return (
    <Button
      variant="ghost"
      size="icon"
      className={cn(className)}
      onClick={() => setSkin(next)}
      title={`${SKIN_LABELS[skin]} skin — switch to ${SKIN_LABELS[next]}`}
      aria-label={`Theme skin: ${SKIN_LABELS[skin]}. Switch to ${SKIN_LABELS[next]}.`}
    >
      <Palette className="size-4" aria-hidden="true" />
    </Button>
  );
}
