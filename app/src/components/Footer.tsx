import React from 'react';
import { View, Text, StyleSheet } from 'react-native';
import { APP_STRINGS, Spacing } from '@/constants/theme';

export const Footer: React.FC = () => {
  return (
    <View style={styles.container}>
      <View style={styles.topBar} />
      <View style={styles.content}>
        <Text style={styles.brandName}>Washington University in St. Louis</Text>
        <Text style={styles.address}>One Brookings Drive, St. Louis, MO 63130</Text>
      </View>
    </View>
  );
};

const styles = StyleSheet.create({
  container: {
    backgroundColor: '#1A1A1A',
    marginTop: Spacing.four,
  },
  topBar: {
    height: 4,
    backgroundColor: '#BA0C2F',
  },
  content: {
    paddingHorizontal: Spacing.four,
    paddingVertical: Spacing.four,
    alignItems: 'center',
    gap: Spacing.one,
  },
  brandName: {
    color: '#FFFFFF',
    fontSize: 14,
    fontWeight: '700',
    textAlign: 'center',
  },
  address: {
    color: '#AAAAAA',
    fontSize: 12,
    textAlign: 'center',
  },
});
