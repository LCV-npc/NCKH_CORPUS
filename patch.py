import re

with open('script.js', 'r', encoding='utf-8') as f:
    content = f.read()

# Try to find the selectArticle function logic that hides buttons
target = 'document.getElementById("btnSaveNer").style.display = "none";\n    nerStatus.textContent = "";\n    return;\n  }'

replacement = 'document.getElementById("btnSaveNer").style.display = "none";\n    document.getElementById("btnAiNer").style.display = "inline-flex";\n    document.getElementById("aiPanel").style.display = "none";\n    nerStatus.textContent = "";\n    return;\n  }'

if target in content:
    content = content.replace(target, replacement)
    with open('script.js', 'w', encoding='utf-8') as f:
        f.write(content)
    print("Replaced successfully")
else:
    print("Target not found. Let's try regex.")
    content = re.sub(r'document\.getElementById\("btnSaveNer"\)\.style\.display = "none";\s*nerStatus\.textContent = "";', 
        'document.getElementById("btnSaveNer").style.display = "none";\n    document.getElementById("btnAiNer").style.display = "inline-flex";\n    document.getElementById("aiPanel").style.display = "none";\n    nerStatus.textContent = "";', content)
    with open('script.js', 'w', encoding='utf-8') as f:
        f.write(content)
    print("Regex replace attempted")
