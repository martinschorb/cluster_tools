import os
import sys
import json
import unittest
import numpy as np

import luigi
import z5py

import nifty.tools as nt
import nifty.graph.rag as nrag
import nifty.distributed as ndist

try:
    from ..base import BaseTest
except ValueError:
    sys.path.append('..')
    from base import BaseTest


class TestGraph(BaseTest):
    input_key = 'volumes/segmentation/watershed'
    output_key = 'graph'

    def check_subresults(self):
        f = z5py.File(self.input_path)
        f_out = z5py.File(self.output_path)
        ds_ws = f[self.input_key]

        full_graph = ndist.Graph(self.output_path, self.output_key)

        shape = ds_ws.shape
        blocking = nt.blocking([0, 0, 0], list(shape),
                               self.block_shape)

        halo = [1, 1, 1]
        for block_id in range(blocking.numberOfBlocks):
            # get the block with the appropriate halo
            # and the corresponding bounding box
            block = blocking.getBlockWithHalo(block_id, halo)
            outer_block, inner_block = block.outerBlock, block.innerBlock
            bb1 = tuple(slice(beg, end) for beg, end in zip(inner_block.begin,
                                                            inner_block.end))
            bb2 = tuple(slice(beg, end) for beg, end in zip(outer_block.begin,
                                                            inner_block.end))
            # check that the rois are correct
            block_key = os.path.join('s0', 'sub_graphs', 'block_%i' % block_id)
            roi_begin = f_out[block_key].attrs['roiBegin']
            roi_end = f_out[block_key].attrs['roiEnd']
            self.assertEqual(inner_block.begin, roi_begin)
            self.assertEqual(inner_block.end, roi_end)

            # load the graph
            graph = ndist.Graph(self.output_path, block_key)
            nodes_deser = ndist.loadNodes(self.output_path, block_key)

            # load the segmentation and check that the nodes
            # are correct
            seg1 = ds_ws[bb1]
            nodes_ws = np.unique(seg1)
            self.assertTrue(np.array_equal(nodes_ws, nodes_deser))

            # compute the rag and check that the graph is correct
            seg2 = ds_ws[bb2]

            # check the graph nodes (only if we have edges)
            if graph.numberOfEdges > 0:
                nodes = graph.nodes()
                nodes_ws2 = np.unique(seg2)
                self.assertTrue(np.array_equal(nodes_ws2, nodes))

            rag = nrag.gridRag(seg2, numberOfLabels=int(seg2.max()) + 1)
            # number of nodes in nifty can be larger
            self.assertGreaterEqual(rag.numberOfNodes, graph.numberOfNodes)
            self.assertEqual(rag.numberOfEdges, graph.numberOfEdges)
            uv_ids = graph.uvIds()
            self.assertTrue(np.array_equal(rag.uvIds(), uv_ids))

            if graph.numberOfEdges == 0:
                continue
            # check the edge ids
            edge_id_key = os.path.join(block_key, 'edgeIds')
            self.assertIn(edge_id_key, f_out)
            edge_ids = f_out[edge_id_key][:]
            self.assertEqual(len(edge_ids), graph.numberOfEdges)
            expected_ids = full_graph.findEdges(uv_ids)
            self.assertTrue(np.array_equal(edge_ids, expected_ids))

    def check_result(self):
        # check shapes
        with z5py.File(self.input_path) as f:
            seg = f[self.input_key]
            seg.n_threads = 8
            seg = seg[:]
            shape = seg.shape
        with z5py.File(self.output_path) as f:
            shape_ = tuple(f.attrs['shape'])
        self.assertEqual(shape, shape_)

        # check graph
        # compute nifty rag
        rag = nrag.gridRag(seg, numberOfLabels=int(seg.max()) + 1)

        # load the graph
        graph = ndist.Graph(self.output_path, self.output_key)

        self.assertEqual(rag.numberOfNodes, graph.numberOfNodes)
        self.assertEqual(rag.numberOfEdges, graph.numberOfEdges)
        self.assertTrue(np.array_equal(rag.uvIds(), graph.uvIds()))

    def test_graph(self):
        from cluster_tools.graph import GraphWorkflow
        task = GraphWorkflow

        task_config = GraphWorkflow.get_config()['initial_sub_graphs']
        task_config['ignore_label'] = False
        with open(os.path.join(self.config_folder, 'initial_sub_graphs.config'),
                  'w') as f:
            json.dump(task_config, f)

        ret = luigi.build([task(input_path=self.input_path,
                                input_key=self.input_key,
                                graph_path=self.output_path,
                                output_key=self.output_key,
                                n_scales=1,
                                config_dir=self.config_folder,
                                tmp_folder=self.tmp_folder,
                                target=self.target,
                                max_jobs=self.max_jobs)], local_scheduler=True)
        self.assertTrue(ret)
        self.check_subresults()
        self.check_result()


if __name__ == '__main__':
    unittest.main()
