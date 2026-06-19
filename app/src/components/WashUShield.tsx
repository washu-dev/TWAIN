import React from 'react';
import { Platform } from 'react-native';

interface WashUShieldProps {
  height?: number;
  // 'color' renders white (for red header), 'original' uses the SVG's own colors
  variant?: 'white' | 'original';
}

export const WashUShield: React.FC<WashUShieldProps> = ({
  height = 72,
  variant = 'white',
}) => {
  const width = height * (160 / 182);

  if (Platform.OS !== 'web') return null;

  // CSS filter to turn any color to white (brightness=0 makes black, invert=1 makes white)
  const whiteFilter = 'brightness(0) invert(1)';

  return (
    <img
      src="/washu-shield.svg"
      width={width}
      height={height}
      alt="Washington University in St. Louis Shield"
      style={{
        objectFit: 'contain',
        filter: variant === 'white' ? whiteFilter : 'none',
      }}
    />
  );
};
