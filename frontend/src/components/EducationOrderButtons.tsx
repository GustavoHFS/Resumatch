import React from 'react';
import { IconArrowDown, IconArrowUp } from './Icons';

export const EducationOrderButtons: React.FC<{
  index: number;
  count: number;
  onMove: (delta: -1 | 1) => void;
}> = ({ index, count, onMove }) => <>
  <button type="button" className="iconBtn" title="Subir formação" aria-label="Subir formação"
    disabled={index === 0} onClick={() => onMove(-1)}><IconArrowUp size={14} /></button>
  <button type="button" className="iconBtn" title="Descer formação" aria-label="Descer formação"
    disabled={index === count - 1} onClick={() => onMove(1)}><IconArrowDown size={14} /></button>
</>;
