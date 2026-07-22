import os
import datetime

folder = 'Kho_Ngu_Lieu_Txt'
files = os.listdir(folder)
files = [f for f in files if f.endswith('.txt')]

# sort files to have consistent numbering
files.sort()

for i, f in enumerate(files):
    filepath = os.path.join(folder, f)
    # get modification time
    mtime = os.path.getmtime(filepath)
    dt = datetime.datetime.fromtimestamp(mtime)
    date_str = dt.strftime('%d%m%Y')
    
    # original name without extension
    name_no_ext = os.path.splitext(f)[0]
    
    # take up to 40 words
    words = name_no_ext.split()
    short_title = " ".join(words[:40])
    
    # sequence number
    stt = f"{i+1:04d}"
    
    new_name = f"{date_str}_{short_title}_{stt}.txt"
    new_filepath = os.path.join(folder, new_name)
    
    # In case of duplicate names
    if not os.path.exists(new_filepath):
        os.rename(filepath, new_filepath)
    else:
        print(f"File {new_name} already exists.")

print(f"Renamed {len(files)} files.")
