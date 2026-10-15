MAX_CHUNK_CHARS = 800


def create_chunks(pages):
    chunks = []

    for page in pages:
        page_number = page["page"]
        text = page["text"]

        paragraphs = [
            part.strip()
            for part in text.split("\n\n")
            if part.strip()
        ]

        current_words = []
        current_length = 0

        for paragraph in paragraphs:
            words = paragraph.split()

            for word in words:
                extra = len(word) + (1 if current_words else 0)

                if (
                    current_words
                    and current_length + extra > MAX_CHUNK_CHARS
                ):
                    chunks.append({
                        "page": page_number,
                        "text": " ".join(current_words)
                    })

                    current_words = [word]
                    current_length = len(word)

                else:
                    current_words.append(word)
                    current_length += extra

        if current_words:
            chunks.append({
                "page": page_number,
                "text": " ".join(current_words)
            })

    return chunks