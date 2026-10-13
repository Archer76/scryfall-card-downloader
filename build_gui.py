from pathlib import Path
core = Path('scryfall_cards_threaded.py').read_text(encoding='utf-8')
core = core.split('\nif __name__ == "__main__":')[0]
ptcg = Path('ptcg_engine.py').read_text(encoding='utf-8')
ptcg = ptcg.replace("if __name__ != '__embedded_ptcg__':\n    import scryfall_cards_threaded as _ptcg_core\n", '')
layer = Path('gui_layer.py').read_text(encoding='utf-8')
ygo = Path('ygo_engine.py').read_text(encoding='utf-8')
result = core + '\n\n' + ptcg + '\n\n' + ygo + '\n\n' + layer
compile(result, 'scryfall_cards_gui.pyw', 'exec')
Path('scryfall_cards_gui.pyw').write_text(result, encoding='utf-8')
print('GUI created: scryfall_cards_gui.pyw')
