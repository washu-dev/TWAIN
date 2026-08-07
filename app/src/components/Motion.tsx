import React, { useEffect, useState } from 'react';
import {
  Animated,
  Easing,
  Platform,
  Pressable,
  PressableProps,
  StyleProp,
  ViewStyle,
} from 'react-native';
import { Motion } from '@/constants/theme';
import { useReducedMotion } from '@/hooks/useReducedMotion';

/**
 * Built on React Native's own Animated, deliberately -- not Reanimated.
 *
 * Reanimated is installed but nothing imported it, so it was tree-shaken out
 * entirely. The moment this file called useSharedValue, the whole Reanimated +
 * worklets runtime started shipping and the web bundle went from 1.38 MB to
 * 2.14 MB: +759 KB, +55%, to pay for a 10pt fade and a 0.97 scale. On a phone on
 * cellular that is roughly an extra second before first paint, on an app whose
 * opening screen is five tiles.
 *
 * Animated is already in the bundle because React Native ships it. It cannot do
 * everything Reanimated can -- no gesture-driven values running on the UI thread --
 * but an opacity ramp and a press spring are indistinguishable, and neither is
 * worth three quarters of a megabyte. Reach for Reanimated again when something
 * genuinely needs to track a finger; then the weight buys something.
 */
const EASE = Easing.bezier(...Motion.easeOut);

// react-native-web has no native driver: asking for one warns per animation and
// falls back to JS anyway. Native platforms get it, where it actually matters.
const USE_NATIVE_DRIVER = Platform.OS !== 'web';

/**
 * The pressable IS the animated node.
 *
 * The obvious shape -- <Pressable><Animated.View style={style}>…</Animated.View>
 * -- silently breaks layout: the caller's `flex: 1` lands on the inner view while
 * the Pressable stays auto-width, so two buttons meant to share a row stop
 * sharing it. Animating the Pressable itself keeps layout and transform on one
 * node, which is also fewer views.
 */
const AnimatedPressable = Animated.createAnimatedComponent(Pressable);

interface RevealProps {
  children: React.ReactNode;
  /** Position in a staggered group. Each sibling waits `index * Motion.stagger`. */
  index?: number;
  /** How far it rises, in points. Small by default -- see the note below. */
  distance?: number;
  style?: StyleProp<ViewStyle>;
}

/**
 * Fades and lifts its children into place once, on mount.
 *
 * The distance is deliberately small (10pt). A long travel reads as a slideshow
 * and, worse, makes the first paint feel late -- the content appears to arrive
 * rather than to be there. What sells "expensive" is the opacity curve, not the
 * displacement.
 *
 * With Reduce Motion on, this renders its children plainly: no opacity ramp
 * either, because a fade IS motion to someone who asked for none.
 */
export const Reveal: React.FC<RevealProps> = ({
  children,
  index = 0,
  distance = 10,
  style,
}) => {
  const reduced = useReducedMotion();
  // useState with a lazy initialiser, not useRef(new Animated.Value(…)).current.
  // The ref spelling is the older idiom but it constructs a throwaway Value on
  // every render, and reading `.current` during render is exactly what the React
  // Compiler's react-hooks/refs rule exists to catch. This gives one instance,
  // created once, with a stable identity and nothing to silence.
  const [progress] = useState(() => new Animated.Value(reduced ? 1 : 0));

  useEffect(() => {
    if (reduced) {
      progress.setValue(1);
      return undefined;
    }
    const delay = index * Motion.stagger;
    const animation = Animated.timing(progress, {
      toValue: 1,
      duration: Motion.smooth,
      delay,
      easing: EASE,
      useNativeDriver: USE_NATIVE_DRIVER,
    });
    let finished = false;
    animation.start(() => {
      finished = true;
    });
    // Safety net, and the important part: a decorative animation must never be
    // what decides whether content is on screen. If the animation is dropped for
    // any reason -- a platform quirk, a backgrounded tab, a future upgrade -- this
    // snaps the element visible shortly after it should already have been. The
    // previous version of this file used Reanimated's withDelay, which never
    // completed on web and left three of four dashboard tiles permanently
    // invisible; the library changed, the lesson did not.
    const failsafe = setTimeout(() => {
      if (!finished) progress.setValue(1);
    }, delay + Motion.smooth + 400);
    return () => {
      animation.stop();
      clearTimeout(failsafe);
    };
  }, [index, progress, reduced]);

  return (
    <Animated.View
      style={[
        style,
        {
          opacity: progress,
          transform: [
            {
              // interpolate rather than arithmetic: with the native driver the
              // whole chain has to be describable to the platform, and a plain
              // multiply in JS is not.
              translateY: progress.interpolate({
                inputRange: [0, 1],
                outputRange: [distance, 0],
              }),
            },
          ],
        },
      ]}
    >
      {children}
    </Animated.View>
  );
};

interface PressableScaleProps extends PressableProps {
  children: React.ReactNode;
  style?: StyleProp<ViewStyle>;
  /** Override the resting scale target on press. */
  scaleTo?: number;
}

/**
 * A Pressable that yields under the finger and springs back.
 *
 * This is the single highest-value bit of polish in the whole pass: touch
 * feedback is what makes an interface feel like a physical object rather than a
 * web page. `activeOpacity` fading a tile to 75% -- what this replaces -- reads as
 * "the element went away", where a scale reads as "the element was pressed".
 *
 * Press-in is a fast timing (a finger arriving is not springy); release is a
 * spring (a surface rebounding is). Getting those two the right way round is most
 * of the effect.
 */
export const PressableScale: React.FC<PressableScaleProps> = ({
  children,
  style,
  scaleTo = Motion.pressScale,
  ...rest
}) => {
  const reduced = useReducedMotion();
  const [scale] = useState(() => new Animated.Value(1));

  return (
    <AnimatedPressable
      style={[style, { transform: [{ scale }] }]}
      onPressIn={() => {
        if (!reduced) {
          Animated.timing(scale, {
            toValue: scaleTo,
            duration: Motion.instant,
            easing: EASE,
            useNativeDriver: USE_NATIVE_DRIVER,
          }).start();
        }
      }}
      onPressOut={() => {
        if (!reduced) {
          Animated.spring(scale, {
            toValue: 1,
            ...Motion.spring,
            useNativeDriver: USE_NATIVE_DRIVER,
          }).start();
        }
      }}
      {...rest}
    >
      {children}
    </AnimatedPressable>
  );
};
