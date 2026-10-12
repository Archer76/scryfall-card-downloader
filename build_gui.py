from pathlib import Path
core = Path('scryfall_cards_threaded.py').read_text(encoding='utf-8')
core = core.split('\nif __name__ == "__main__":')[0]
layer = Path('gui_layer.py').read_text(encoding='utf-8')
result = core + '\n\n' + layer
compile(result, 'scryfall_cards_gui.pyw', 'exec')
Path('scryfall_cards_gui.pyw').write_text(result, encoding='utf-8')
print('GUI created: scryfall_cards_gui.pyw')
