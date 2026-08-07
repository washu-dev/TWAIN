import React, { useEffect } from 'react';
import { Pressable, PressableProps, StyleProp, ViewStyle } from 'react-native';
import Animated, {
  Easing,
  useAnimatedStyle,
  useSharedValue,
  withSpring,
  withTiming,
} from 'react-native-reanimated';
import { Motion } from '@/constants/theme';
import { useReducedMotion } from '@/hooks/useReducedMotion';

const EASE = Easing.bezier(...Motion.easeOut);

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
  const progress = useSharedValue(reduced ? 1 : 0);

  useEffect(() => {
    if (reduced) {
      progress.value = 1;
      return;
    }
    const delay = index * Motion.stagger;
    // The stagger is a plain timer, not withDelay(). On react-native-web the
    // delayed animation never completed: index 0 (no delay) faded in correctly
    // while every delayed sibling stayed at opacity 0 -- three of four tiles on
    // the dashboard were invisible.
    const start = setTimeout(() => {
      progress.value = withTiming(1, { duration: Motion.smooth, easing: EASE });
    }, delay);
    // Safety net, and the important part: a decorative animation must never be
    // what decides whether content is on screen. If the animation engine misses
    // for any reason -- a platform quirk, a backgrounded tab, a future upgrade --
    // this snaps the element visible shortly after it should already have been.
    const failsafe = setTimeout(() => {
      if (progress.value < 1) progress.value = 1;
    }, delay + Motion.smooth + 400);
    return () => {
      clearTimeout(start);
      clearTimeout(failsafe);
    };
  }, [index, progress, reduced]);

  const animated = useAnimatedStyle(() => ({
    opacity: progress.value,
    transform: [{ translateY: (1 - progress.value) * distance }],
  }));

  return <Animated.View style={[style, animated]}>{children}</Animated.View>;
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
 * Press-in is a fast linear-ish timing (a finger arriving is not springy);
 * release is a spring (a surface rebounding is). Getting those two the same way
 * round is most of the effect.
 */
export const PressableScale: React.FC<PressableScaleProps> = ({
  children,
  style,
  scaleTo = Motion.pressScale,
  ...rest
}) => {
  const reduced = useReducedMotion();
  const scale = useSharedValue(1);

  const animated = useAnimatedStyle(() => ({ transform: [{ scale: scale.value }] }));

  // react-hooks/immutability flags `scale.value = …` because the React Compiler
  // treats values captured from render as immutable. It is a false positive for
  // Reanimated: a shared value is a mutable box that lives OUTSIDE React's render
  // state precisely so it can be driven from an event handler without a re-render
  // -- that is the entire point of useSharedValue, and the assignment is the
  // documented API. Disabled at the two assignments rather than for the file, so a
  // genuine mutation elsewhere in here would still be caught.
  return (
    <AnimatedPressable
      style={[style, animated]}
      onPressIn={() => {
        if (!reduced) {
          // eslint-disable-next-line react-hooks/immutability
          scale.value = withTiming(scaleTo, { duration: Motion.instant, easing: EASE });
        }
      }}
      onPressOut={() => {
        // eslint-disable-next-line react-hooks/immutability
        if (!reduced) scale.value = withSpring(1, Motion.spring);
      }}
      {...rest}
    >
      {children}
    </AnimatedPressable>
  );
};
