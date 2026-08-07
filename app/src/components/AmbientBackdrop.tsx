import React from 'react';
import { StyleSheet, View } from 'react-native';
import { LinearGradient } from 'expo-linear-gradient';
import { Gradients } from '@/constants/theme';

/**
 * The faint warm wash at the top of a screen.
 *
 * This is the "ambiance": a brand-tinted gradient at 5.5% opacity fading to
 * nothing over the first 260pt of the page. It is meant to be unnoticeable as a
 * shape -- if you can see where it ends, it is too strong. What it buys is that
 * white surfaces laid on top read as lit rather than as paper, which is most of
 * the difference between a form and a product.
 *
 * `pointerEvents="none"` because it spans the content: without it this would eat
 * every tap on the top third of the screen. Absolutely positioned and rendered
 * first, so it sits behind siblings without needing zIndex.
 */
export const AmbientBackdrop: React.FC<{ height?: number }> = ({ height = 260 }) => (
  <View style={[styles.container, { height }]} pointerEvents="none">
    <LinearGradient
      colors={Gradients.ambient}
      locations={Gradients.ambientLocations}
      style={StyleSheet.absoluteFill}
    />
  </View>
);

const styles = StyleSheet.create({
  container: {
    position: 'absolute',
    top: 0,
    left: 0,
    right: 0,
  },
});
