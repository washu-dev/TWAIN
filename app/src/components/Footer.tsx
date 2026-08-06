import React from 'react';
import { View, Text, StyleSheet } from 'react-native';
import { Colors, Spacing } from '@/constants/theme';

const C = Colors.light;

export const Footer: React.FC = () => {
  return (
    <View style={styles.container}>
      <View style={styles.topBar} />
      {/* One line. The postal address was ~90pt of every screen, phone included,
          and nobody opens a simulation planner to find out where St. Louis is. */}
      <View style={styles.content}>
        <Text style={styles.brandName}>Washington University in St. Louis</Text>
      </View>
    </View>
  );
};

const styles = StyleSheet.create({
  container: {
    backgroundColor: C.surfaceDark,
    marginTop: Spacing.four,
  },
  topBar: {
    height: 4,
    backgroundColor: C.washuRed,
  },
  content: {
    paddingHorizontal: Spacing.four,
    paddingVertical: Spacing.two,
    alignItems: 'center',
  },
  brandName: {
    color: C.textOnDark,
    fontSize: 13,
    fontWeight: '600',
    textAlign: 'center',
  },
});
