"""Chunking and retrieval over authorized profile documents (pure, no I/O)."""
import unittest

from app import doc_index


class ChunkTests(unittest.TestCase):
    def test_headings_become_separate_chunks_with_line_ranges(self):
        text='# A\nline a\n## B\nline b1\nline b2\n'
        chunks=doc_index.split_chunks(text)
        self.assertEqual([c['heading'] for c in chunks],['A','B'])
        self.assertEqual((chunks[0]['start_line'],chunks[0]['end_line']),(1,2))
        self.assertIn('line b2',chunks[1]['text'])

    def test_a_long_section_is_split_on_line_boundaries_and_capped(self):
        text='## Big\n'+'\n'.join('x'*100 for _ in range(20))
        chunks=doc_index.split_chunks(text,max_chars=300)
        self.assertGreater(len(chunks),1)
        self.assertTrue(all(len(c['text'])<=300 for c in chunks))
        self.assertTrue(all(c['heading']=='Big' for c in chunks))

    def test_a_single_over_long_line_is_also_split(self):
        text='## One\n'+'y'*10000+'\n'
        chunks=doc_index.split_chunks(text,max_chars=4000)
        self.assertGreater(len(chunks),1)
        self.assertTrue(all(len(c['text'])<=4000 for c in chunks))
        # The over-long line is on line 2; every piece must point back at it.
        self.assertTrue(all(c['start_line']==2 for c in chunks if 'y' in c['text']))
        self.assertIn('y'*10000,''.join(c['text'] for c in chunks))

    def test_the_chunk_cap_reports_what_was_dropped(self):
        text='\n'.join('## s%d\nline' % i for i in range(450))
        chunks,dropped=doc_index.split_chunks_with_drops(text,limit=400)
        self.assertEqual(len(chunks),400)
        self.assertEqual(dropped,50)
        # The plain helper still caps, so older callers do not grow unbounded.
        self.assertEqual(len(doc_index.split_chunks(text,limit=400)),400)

    def test_empty_and_whitespace_only_documents_produce_no_chunks(self):
        self.assertEqual(doc_index.split_chunks(''),[])
        self.assertEqual(doc_index.split_chunks('\n\n  \n'),[])

    def test_chunk_hash_changes_with_content(self):
        self.assertNotEqual(doc_index.chunk_sha('a'),doc_index.chunk_sha('b'))
        self.assertEqual(doc_index.chunk_sha('a'),doc_index.chunk_sha('a'))


class RetrievalTests(unittest.TestCase):
    def _chunks(self):
        return doc_index.split_chunks(
            '# 算法计划\n使用 Mid360 与 PCD 评分\n'
            '## 版本控制\nGit tag v1.2 已发布\n'
            '## 测试记录\n2026-09-07 冒烟通过\n')

    def test_heading_and_body_terms_rank_the_relevant_chunk_first(self):
        ranked=doc_index.rank_chunks(self._chunks(),'版本控制现在是什么状态')
        self.assertEqual(ranked[0]['heading'],'版本控制')

    def test_cjk_relevance_is_casefolded_and_deterministic(self):
        chunks=self._chunks()
        first=doc_index.rank_chunks(chunks,'PCD 评分怎么算')
        second=doc_index.rank_chunks(chunks,'PCD 评分怎么算')
        self.assertEqual([c['heading'] for c in first],[c['heading'] for c in second])
        self.assertEqual(first[0]['heading'],'算法计划')

    def test_an_empty_question_keeps_document_order(self):
        ranked=doc_index.rank_chunks(self._chunks(),'')
        self.assertEqual([c['heading'] for c in ranked],['算法计划','版本控制','测试记录'])

    def test_limit_is_respected(self):
        self.assertEqual(len(doc_index.rank_chunks(self._chunks(),'q',limit=1)),1)


if __name__=='__main__':
    unittest.main()
